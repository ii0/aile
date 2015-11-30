import collections
import itertools

import numpy as np
import sklearn.cluster
import networkx as nx

import _kernel as _ker
import dtw


def to_rows(d):
    """Make a square matrix with rows equal to 'd'.

    >>> print to_rows(np.array([1,2,3,4]))
    [[1 2 3 4]
     [1 2 3 4]
     [1 2 3 4]
     [1 2 3 4]]
     """
    return np.tile(d, (len(d), 1))


def to_cols(d):
    """Make a square matrix with columns equal to 'd'.

    >>> print ker.to_cols(np.array([1,2,3,4]))
    [[1 1 1 1]
     [2 2 2 2]
     [3 3 3 3]
     [4 4 4 4]]
    """
    return np.tile(d.reshape(len(d), -1), (1, len(d)))


def normalize_kernel(K):
    """New kernel with unit diagonal.

    K'[i, j] = K[i, j]/sqrt(K[i,i]*K[j,j])
    """
    d = np.diag(K).copy()
    d[d == 0] = 1.0
    return K/np.sqrt(to_rows(d)*to_cols(d))


def kernel_to_distance(K):
    """Build a distance matrix.

    From the dot product:
        |u - v|^2 = (u - v)(u - v) = u^2 + v^2 - 2uv
    """
    d = np.diag(K)
    return np.sqrt(to_rows(d) + to_cols(d) - 2*K)


def tree_size_distance(page_tree):
    """Build a distance matrix comparing subtree sizes.

    If T1 and T2 are trees and N1 and N2 the number of nodes within:
        |T1 - T2| = |N1 - N2|/(N1 + N2)
    Since:
        N1 >= 1
        N2 >= 1
    Then:
        0 <= |T1 - T2| < 1
    """
    s = page_tree.tree_size()
    a = to_cols(s).astype(float)
    b = to_rows(s).astype(float)
    return np.abs(a - b)/(a + b)


def cut_descendants(G, page_tree):
    for src in G.nodes_iter():
        m = page_tree.match[src]
        if m >= 0:
            for tgt in range(src+1, m):
                if tgt in G:
                    try:
                        for cut_edge in nx.minimum_edge_cut(G, src, tgt):
                            G.remove_edge(*cut_edge)
                    except nx.NetworkXUnbounded:
                        print 'Could not separate: ', src, tgt
    return nx.connected_components(G)


def labels_to_clusters(labels):
    return [np.flatnonzero(labels==label) for label in np.unique(labels)
            if label != -1]


def clusters_to_labels(clusters, n_samples):
    labels = np.repeat(-1, n_samples)
    for i, c in enumerate(clusters):
        for j in c:
            labels[j] = i
    return labels


def boost(d, k=2):
    return 1 - (1 - d)**k


class TreeClustering(object):
    def __init__(self, page_tree):
        self.page_tree = page_tree

    def fit_predict(self, X, min_cluster_size=6,
                    separate_descendants=False):
        D = X.copy() + boost(tree_size_distance(self.page_tree), 4)
        clt = sklearn.cluster.DBSCAN(
            eps=1.0, min_samples=min_cluster_size, metric='precomputed')
        self.clusters = []
        for c in labels_to_clusters(clt.fit_predict(D)):
            if len(c) >= min_cluster_size:
                if separate_descendants:
                    self.clusters += cut_descendants(
                        self.neighbors(D, c), self.page_tree)
                else:
                    self.clusters.append(c)
        self.labels = clusters_to_labels(self.clusters, D.shape[0])
        return self.labels

    def neighbors(self, D, nodes=None, k=50):
        if nodes is None:
            nodes = np.arange(D.shape[0])
        E = D[nodes,:][:,nodes]
        k = min(k+1, len(nodes))
        K = np.argsort(E, axis=1)
        G = nx.Graph()
        for i in range(K.shape[0]):
            ni = nodes[i]
            pi = self.page_tree.parents[ni]
            if pi != -1:
                for j in range(i+1, K.shape[0]):
                    nj = nodes[j]
                    pj = self.page_tree.parents[nj]
                    if pj == pi:
                        G.add_edge(
                            min(ni, nj), max(ni, nj),
                            capacity=1.0/(np.sqrt(2) + 1 - E[i, j]))
            p = q = 0
            while p < K.shape[1] and q < k:
                j = K[i, p]
                nj = nodes[j]
                if E[i, j] == 0:
                    cap = np.inf
                else:
                    cap = 1.0/(np.sqrt(2) + 1 - E[i, j])
                    q += 1
                G.add_edge(min(ni, nj), max(ni, nj), capacity=cap)
                p += 1
        return G

def cluster(page_tree, K):
    """Asign to each node in the tree a cluster label.

    Returns: for each node a label id. Label ID -1 means that the node
    is an outlier (it isn't part of any cluster).
    """
    return TreeClustering(page_tree).fit_predict(
        kernel_to_distance(normalize_kernel(K)))


def extract_label(ptree, labels, label_to_extract):
    """Extract all forests inside the labeled PageTree that are marked or have
    a sibling that is marked with label_to_extract.

    Returns: a list of tuples, where each tuple are the roots of the extracted
    subtrees.
    """
    roots = []
    i = 0
    while i < len(labels):
        children = ptree.children(i)
        if np.any(labels[children] == label_to_extract):
            first = None
            item = []
            for c in children:
                m = labels[c]
                if m != -1:
                    if first is None:
                        first = m
                    elif m == first:
                        roots.append(tuple(item))
                        item = []
                    item.append(c)
            if item:
                roots.append(tuple(item))
            i = ptree.match[i]
        else:
            i += 1
    return roots


def filter_children_labels(ptree, labels):
    """Assign children the labels of their parents, if any"""
    labels = labels.copy()
    for i, l in enumerate(labels):
        if l != -1:
            labels[i:max(i, ptree.match[i])] = l
    return labels


def filter_extracted_labels(labels, extracted):
    """Mark labels already extracted"""
    labels = labels.copy()
    for forest in extracted:
        for root in forest:
            labels[root] = -1
    return labels


def score_labels(ptree, labels):
    """Assign an score for each label"""
    scores = collections.defaultdict(int)
    for i, l in enumerate(labels):
        if l != -1:
            scores[l] += max(0, ptree.match[i] - i + 1)
    return scores


def extract_trees(ptree, labels, min_n_trees=6):
    """Extract the repeating trees.

    We cannot use the cluster labels as is because:
        1. If a tree repeats not only its root is assigned a label,
        most of its children too.
        2. A repating patter can be made of several distinct trees.

    The algorithm to extract the repeating trees goes as follows:
        1. Determine the label that covers most children on the page
        2. If a node with that label has siblings, extract the siblings too,
           even if they have other labels.
    """
    labels = filter_children_labels(ptree, labels)
    scores = score_labels(ptree, labels)
    trees = []
    for l, s in sorted(scores.items(), key=lambda kv: kv[1], reverse=True):
        t = extract_label(ptree, labels, l)
        if len(t) >= min_n_trees:
            trees.append((s, t))
            labels = filter_extracted_labels(labels, t)
    return trees


def path_distance(path_1, path_2):
    """Compute the prefix distance between the two paths.

    >>> p1 = [1, 0, 3, 4, 5, 6]
    >>> p2 = [1, 0, 2, 2, 2, 2, 2, 2]
    >>> print path_distance(p1, p2)
    6
    """
    d = max(len(path_1), len(path_2))
    for a, b in zip(path_1, path_2):
        if a != b:
            break
        d -= 1
    return d


def pairwise_path_distance(path_seq_1, path_seq_2):
    """Compute all pairwise distances between paths in path_seq_1 and
    path_seq_2"""
    N1 = len(path_seq_1)
    N2 = len(path_seq_2)
    D = np.zeros((N1, N2))
    for i in range(N1):
        q1 = path_seq_1[i]
        for j in range(N2):
            D[i, j] = path_distance(q1, path_seq_2[j])
    return D


def extract_path_seq_1(ptree, labels, forest):
    paths = []
    for root in forest:
        for path in ptree.prefixes_at(root):
            paths.append((path[0], labels[path].tolist()))
    return paths


def extract_path_seq(ptree, trees, labels):
    all_paths = []
    for tree in trees:
        paths = extract_path_seq_1(ptree, labels, tree)
        all_paths.append(paths)
    return all_paths


def find_cliques(G, min_size):
    """Find all cliques in G above a given size.

    If a node is part of a larger clique is deleted from the smaller ones.
    Returns:
        A dictionary mapping nodes to clique ID
    """
    cliques = []
    for K in nx.find_cliques(G):
        if len(K) >= min_size:
            cliques.append(set(K))
    cliques.sort(reverse=True, key=lambda x: len(x))
    L = set()
    for K in cliques:
        K -= L
        L |= K
    cliques = [J for J in cliques if len(J) >= min_size]
    node_to_clique = {}
    for i, K in enumerate(cliques):
        for node in K:
            if node not in node_to_clique:
                node_to_clique[node] = i
    return node_to_clique


def match_graph(all_paths):
    """Build a graph where n1 and n2 share an edge if they have
    been matched using DTW"""
    G = nx.Graph()
    for path_set_1, path_set_2 in itertools.combinations(all_paths, 2):
        n1, p1 = zip(*path_set_1)
        n2, p2 = zip(*path_set_2)
        D = pairwise_path_distance(p1, p2)
        DTW = dtw.from_distance(D)
        a1, a2 = dtw.path(DTW)
        m = dtw.match(a1, a2, D)
        for i, j in enumerate(m):
            if j != -1:
                G.add_edge(n1[i], n2[j])
    return G


def align_items(ptree, trees, node_to_clique):
    n_cols = max(node_to_clique.values()) + 1
    items = np.zeros((len(trees), n_cols), dtype=int) - 1
    for i, tree in enumerate(trees):
        for root in tree:
            for c in range(root, max(root + 1, ptree.match[root])):
                try:
                    items[i, node_to_clique[c]] = c
                except KeyError:
                    pass
    return items


def extract_items(ptree, trees, labels):
    return align_items(
        ptree,
        trees,
        find_cliques(
            match_graph(extract_path_seq(ptree, trees, labels)),
            0.5*len(trees))
    )


ItemTable = collections.namedtuple('ItemTable', ['roots', 'fields'])


class ItemExtract(object):
    def __init__(self, page_tree, k_max_depth=2, k_decay=0.5):
        """Perform all extraction operations in sequence.

        Parameters:
            k_*: parameters to kernel computation
            c_*: parameters to clustering
        """
        self.page_tree = page_tree
        self.kernel = _ker.kernel(page_tree, max_depth=k_max_depth, decay=k_decay)
        self.labels = cluster(page_tree, self.kernel)
        self.trees = extract_trees(page_tree, self.labels)
        self.items = [ItemTable(t, extract_items(page_tree, t, self.labels))
                      for (s, t) in self.trees]
        self.item_fragments = [
            ItemTable([page_tree.fragment_index(np.array(root)) for root in t],
                      page_tree.fragment_index(i))
            for t, i in self.items]
