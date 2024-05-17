from collections import Counter
import scipy.sparse as sp
import torch
import torch.nn.functional as F

import torch_geometric.transforms as T
from torch_geometric.datasets import Planetoid
from torch_geometric.utils import add_self_loops, remove_self_loops, to_undirected, degree

from ogb.nodeproppred import PygNodePropPredDataset

from sklearn.preprocessing import StandardScaler
from tqdm import tqdm

from graph_coarsening.graph_utils import *

from graph_coarsening.graph_utils import zero_diag
from graphmae.datasets.wrapper import Wrapper
from graphmae.transform.posec import AddRandomWalkPE
from load_data import load_data
from preprocess_data import process_data
import warnings

warnings.filterwarnings("ignore")


def scale_feats(x):
    scaler = StandardScaler()
    feats = x.numpy()
    scaler.fit(feats)
    feats = torch.from_numpy(scaler.transform(feats)).float()
    return feats


def coarsen_graph(data, coarse_layer, rate, method, transform):
    node_layer_dicts = []
    super_layer_features = []
    super_layer_adjs = []
    proj_layer_matrices = []
    pe_layer = []
    adj = sp.coo_matrix((np.ones(data.edge_index.shape[1]), (data.edge_index[0], data.edge_index[1])),
                        shape=(data.num_nodes, data.num_nodes),
                        dtype=np.float32)
    # initialize the coarse input
    proj_layer_matrices.append([])
    node_layer_dicts.append([])
    super_layer_features.append(data.x)
    super_layer_adjs.append(adj)
    for i in range(1, coarse_layer):
        last_layer_adj = zero_diag(super_layer_adjs[-1])
        last_layer_feature = super_layer_features[-1]
        proj_matrix, super_feature, super_adj, node_dict = process_data(last_layer_adj.shape[0], last_layer_feature,
                                                                        last_layer_adj, rate, method)
        # to_undirected
        super_adj = np.maximum(super_adj, super_adj.T)
        # transform the super_adj to coo format
        super_adj = sp.coo_matrix(super_adj)
        edge_index = torch.tensor(np.array([super_adj.row, super_adj.col]), dtype=torch.long)
        pe = transform(edge_index, num_nodes=super_adj.shape[0])
        proj_layer_matrices.append(proj_matrix)
        super_layer_features.append(super_feature)
        super_layer_adjs.append(super_adj)
        node_layer_dicts.append(node_dict)
        pe_layer.append(pe)
    """
    for i in range(coarse_layer):
        G = nx.to_networkx_graph(super_layer_adjs[i])
        pos_G = nx.spring_layout(G) 
        nx.draw(G, pos_G, with_labels=True, node_size=300, node_color='skyblue', font_size=10)
        plt.title("Graph G")
        plt.show()
    """
    return proj_layer_matrices, super_layer_adjs, super_layer_features, node_layer_dicts, pe_layer


def load_dataset(args, dataset_name):
    if dataset_name == "ogbn-arxiv":
        dataset = PygNodePropPredDataset(name='ogbn-arxiv', root="../data")
        graph = dataset[0]
        num_nodes = graph.x.shape[0]
        graph.edge_index = to_undirected(graph.edge_index)
        graph.edge_index = remove_self_loops(graph.edge_index)[0]
        graph.edge_index = add_self_loops(graph.edge_index)[0]
        split_idx = dataset.get_idx_split()
        train_idx, val_idx, test_idx = split_idx["train"], split_idx["valid"], split_idx["test"]
        if not torch.is_tensor(train_idx):
            train_idx = torch.as_tensor(train_idx)
            val_idx = torch.as_tensor(val_idx)
            test_idx = torch.as_tensor(test_idx)
        train_mask = torch.full((num_nodes,), False).index_fill_(0, train_idx, True)
        val_mask = torch.full((num_nodes,), False).index_fill_(0, val_idx, True)
        test_mask = torch.full((num_nodes,), False).index_fill_(0, test_idx, True)
        graph.train_mask, graph.val_mask, graph.test_mask = train_mask, val_mask, test_mask
        graph.y = graph.y.view(-1)
        graph.x = scale_feats(graph.x)
    else:
        dataset = Planetoid(f"../data/{dataset_name}", dataset_name, transform=T.NormalizeFeatures())
        graph = dataset[0]
        graph.edge_index = remove_self_loops(graph.edge_index)[0]
        graph.edge_index = add_self_loops(graph.edge_index)[0]

    num_features = dataset.num_features
    num_classes = dataset.num_classes
    wrapped_dataset = Wrapper(graph)
    proj_matrices, super_adjs, super_features, node_dicts, pe = coarsen_graph(graph, args.coarse_layer,
                                                                              args.coarse_rate, args.coarse_type, None)
    wrapped_dataset.put_item((proj_matrices, super_adjs, super_features, node_dicts, pe))

    return wrapped_dataset, (num_features, num_classes)


def load_graph_classification_dataset(args, deg4feat=False):
    dataset = load_data(args)
    dataset = list(dataset)
    coarse_layer = args.coarse_layer
    coarse_rate = args.coarse_rate
    coarse_type = args.coarse_type
    pe_dim = args.pe_dim
    graph = dataset[0]
    node_dicts = []
    super_features = []
    super_adjs = []
    proj_matrices = []
    pe_list = []
    if graph.x is None:
        if graph.y and not deg4feat and args.dataset != "REDDIT-BINARY":
            print("Use node label as node features")
            feature_dim = 0
            for g in dataset:
                feature_dim = max(feature_dim, int(g.y.max().item()))

            feature_dim += 1
            for i, g in enumerate(dataset):
                node_label = g.y.view(-1)
                feat = F.one_hot(node_label, num_classes=int(feature_dim)).float()
                dataset[i].x = feat
        else:
            print("Using degree as node features")
            feature_dim = 0
            degrees = []
            for g in dataset:
                feature_dim = max(feature_dim, degree(g.edge_index[0]).max().item())
                degrees.extend(degree(g.edge_index[0]).tolist())
            MAX_DEGREES = 400

            oversize = 0
            for d, n in Counter(degrees).items():
                if d > MAX_DEGREES:
                    oversize += n
            # print(f"N > {MAX_DEGREES}, #NUM: {oversize}, ratio: {oversize/sum(degrees):.8f}")
            feature_dim = min(feature_dim, MAX_DEGREES)

            feature_dim += 1
            for i, g in enumerate(dataset):
                degrees = degree(g.edge_index[0])
                degrees[degrees > MAX_DEGREES] = MAX_DEGREES
                degrees = torch.Tensor([int(x) for x in degrees.numpy().tolist()])
                feat = F.one_hot(degrees.to(torch.long), num_classes=int(feature_dim)).float()
                g.x = feat
                dataset[i] = g

    else:
        print("******** Use `attr` as node features ********")
    feature_dim = int(graph.num_features)

    labels = torch.tensor([x.y for x in dataset])
    transform = AddRandomWalkPE(pe_dim)
    num_classes = torch.max(labels).item() + 1
    for i, g in enumerate(dataset):
        dataset[i].edge_index = remove_self_loops(dataset[i].edge_index)[0]
        dataset[i].edge_index = add_self_loops(dataset[i].edge_index)[0]
    # dataset = [(g, g.y) for g in dataset]
    wrapped_dataset = Wrapper(dataset)
    for data in tqdm(dataset, desc="preprocess data"):
        proj_layer_matrices, super_layer_adjs, super_layer_features, node_layer_dicts, pe = (
            coarsen_graph(data, coarse_layer, coarse_rate, coarse_type, transform))
        proj_matrices.append(proj_layer_matrices)
        super_adjs.append(super_layer_adjs)
        super_features.append(super_layer_features)
        node_dicts.append(node_layer_dicts)
        pe_list.append(pe)

    wrapped_dataset.put_item((proj_matrices, super_adjs, super_features, node_dicts, pe_list))
    print(f"******** # Num Graphs: {len(dataset)}, # Num Feat: {feature_dim}, # Num Classes: {num_classes} ********")
    return wrapped_dataset, (feature_dim, num_classes)
