import logging
import time

from tqdm import tqdm
import numpy as np
import torch
from torch_geometric.loader import DataLoader

from sklearn.model_selection import StratifiedKFold, GridSearchCV
from sklearn.svm import SVC
from sklearn.metrics import f1_score

from graphmae.utils import (
    build_args,
    create_optimizer,
    set_random_seed,
    TBLogger,
    get_current_lr,
    load_best_configs, save_result,
)
from graphmae.datasets.data_util import load_graph_classification_dataset
from graphmae.models import build_model

from graphmae.utils import get_layer_loss, get_coarse_proj, get_coarse_edge, get_encoder_out, get_layer_feature, \
    get_mask_list, recover_mask, get_mask_edge, adjust_recover_rate


def graph_classification_evaluation(model, pooler, dataloader, device, coarse_layer):
    model.eval()
    x_list = []
    y_list = []
    with torch.no_grad():
        for i, batch_g in enumerate(dataloader):
            batch_g = batch_g.to(device)
            feat = batch_g.x
            labels = batch_g.y.cpu()
            coarse_proj, coarse_batch = get_coarse_proj(batch_g, coarse_layer, device)
            coarse_edge = get_coarse_edge(batch_g, coarse_layer, device)
            out = get_encoder_out(batch_g, model.encoders, feat, pooler, coarse_edge, coarse_proj, coarse_batch,
                                  coarse_layer, args.last_enc, device)
            y_list.append(labels.numpy())
            x_list.append(out.cpu().numpy())
    x = np.concatenate(x_list, axis=0)
    y = np.concatenate(y_list, axis=0)
    test_f1, test_std = evaluate_graph_embeddings_using_svm(x, y)
    print(f"#Test_f1: {test_f1:.4f}±{test_std:.4f}")
    return test_f1


def evaluate_graph_embeddings_using_svm(embeddings, labels):
    result = []
    kf = StratifiedKFold(n_splits=10, shuffle=True, random_state=0)

    for train_index, test_index in kf.split(embeddings, labels):
        x_train = embeddings[train_index]
        x_test = embeddings[test_index]
        y_train = labels[train_index]
        y_test = labels[test_index]
        params = {"C": [1e-3, 1e-2, 1e-1, 1, 10]}
        svc = SVC(random_state=42)
        clf = GridSearchCV(svc, params)
        clf.fit(x_train, y_train)

        preds = clf.predict(x_test)
        f1 = f1_score(y_test, preds, average="micro")
        result.append(f1)
    test_f1 = np.mean(result)
    test_std = np.std(result)

    return test_f1, test_std


def pretrain(model, dataloaders, optimizer, max_epoch, device, scheduler, coarse_layer, mask_edge, recover_rate,
             logger=None):
    train_loader, eval_loader = dataloaders

    epoch_iter = tqdm(range(max_epoch))

    for epoch in epoch_iter:
        model.train()
        loss_list = []
        # recover rate decay
        recover_rate = adjust_recover_rate(recover_rate, epoch, max_epoch * args.epoch_rate, args.gamma)
        for batch in train_loader:
            batch_g = batch
            batch_g = batch_g.to(device)
            x = batch_g.x
            en_feature_x = x.clone()
            model.train()
            super_feats = []
            coarse_proj, coarse_batch = get_coarse_proj(batch_g, coarse_layer, device)
            coarse_edge = get_coarse_edge(batch_g, coarse_layer, device)
            coarse_feat = get_layer_feature(x, coarse_proj, coarse_layer, device)
            mask_nodes, token_nodes = model.encoding_mask_noise(batch.super_feature[-1].shape[0], device)
            mask_nodes_list, token_nodes_list = get_mask_list(mask_nodes, token_nodes, coarse_proj, coarse_layer,
                                                              device)
            if recover_rate > 0:
                mask_nodes_list, token_nodes_list = recover_mask(mask_nodes_list, token_nodes_list,
                                                                 coarse_layer, recover_rate)
            mask_node_init = torch.where(mask_nodes_list[0] == 0)[0]
            token_node_init = torch.where(token_nodes_list[0] == 0)[0]
            # get noise node
            noise_node = torch.tensor(list(set(mask_node_init.tolist()) - set(token_node_init.tolist())), device=device)
            noise_to_be_chosen = torch.randperm(x.shape[0], device=device)[:len(noise_node)]
            if noise_to_be_chosen.numel() > 0:
                en_feature_x[noise_node] = x[noise_to_be_chosen]
            en_feature_x[token_node_init] = 0.0
            en_feature_x[token_node_init] += model.enc_mask_token
            # encoder
            for i in range(1, coarse_layer + 1):
                edge_index = coarse_edge[i - 1]
                if mask_edge:
                    edge_index = get_mask_edge(edge_index, mask_nodes_list[i - 1])
                if i != coarse_layer or args.last_enc != "transformer":
                    feat, _ = model.encoders[i - 1](en_feature_x, edge_index, return_hidden=True)
                else:
                    feat = model.encoders[i - 1](en_feature_x, batch_g.pe[0], coarse_batch[i - 1],
                                                 mask_nodes_list[i - 1])
                super_feats.append(feat)
                if i != coarse_layer:
                    proj = coarse_proj[i - 1].to(device)
                    en_feature_x = torch.matmul(proj, feat)
            de_feature_x = super_feats[-1]
            # decoder
            for i in range(coarse_layer - 1, -1, -1):
                edge_index = coarse_edge[i]
                # skip connection
                if i != coarse_layer - 1:
                    de_feature_x = de_feature_x + super_feats[i] * mask_nodes_list[i].view(-1, 1)
                de_feature_x, _ = model.decoders[i](de_feature_x, edge_index, return_hidden=True)
                # loss += get_layer_loss(model, coarse_feat[i], de_feature_x, mask_nodes_list[i], (i == 0))
                if i != 0:
                    proj = coarse_proj[i - 1].to(device)
                    de_feature_x = torch.matmul(proj.T, de_feature_x)

            loss = get_layer_loss(model, coarse_feat[0], de_feature_x, mask_nodes_list[0], True)
            loss_dict = {"loss": loss.item()}

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            loss_list.append(loss.item())
            if logger is not None:
                loss_dict["lr"] = get_current_lr(optimizer)
                logger.note(loss_dict, step=epoch)
        if scheduler is not None:
            scheduler.step()
        epoch_iter.set_description(f"Epoch {epoch} | train_loss: {np.mean(loss_list):.4f}")

    return model


def main(args):
    device = args.device if args.device >= 0 else "cpu"
    seeds = args.seeds
    dataset_name = args.dataset
    max_epoch = args.max_epoch
    max_epoch_f = args.max_epoch_f
    num_hidden = args.num_hidden
    num_layers = args.num_layers
    encoder_type = args.encoder
    decoder_type = args.decoder
    replace_rate = args.replace_rate

    optim_type = args.optimizer
    loss_fn = args.loss_fn

    lr = args.lr
    weight_decay = args.weight_decay
    lr_f = args.lr_f
    weight_decay_f = args.weight_decay_f
    linear_prob = args.linear_prob
    load_model = args.load_model
    save_model = args.save_model
    logs = args.logging
    use_scheduler = args.scheduler
    pooler = args.pooling
    deg4feat = args.deg4feat
    batch_size = args.batch_size

    data, (num_features, num_classes) = load_graph_classification_dataset(args, deg4feat=deg4feat)
    args.num_features = num_features

    train_loader = DataLoader(data, batch_size=batch_size, pin_memory=True)
    eval_loader = DataLoader(data, batch_size=batch_size, shuffle=False)

    acc_list = []
    start_time = time.time()
    for i, seed in enumerate(seeds):
        print(f"####### Run {i} for seed {seed}")
        set_random_seed(seed)

        if logs:
            logger = TBLogger(
                name=f"{dataset_name}_loss_{loss_fn}_rpr_{replace_rate}_nh_{num_hidden}_nl_{num_layers}_lr_{lr}_mp_{max_epoch}_mpf_{max_epoch_f}_wd_{weight_decay}_wdf_{weight_decay_f}_{encoder_type}_{decoder_type}")
        else:
            logger = None

        model = build_model(args)
        model.to(device)
        optimizer = create_optimizer(optim_type, model, lr, weight_decay)

        if use_scheduler:
            logging.info("Use schedular")
            scheduler = lambda epoch: (1 + np.cos((epoch) * np.pi / max_epoch)) * 0.5
            # scheduler = lambda epoch: epoch / warmup_steps if epoch < warmup_steps \
            # else ( 1 + np.cos((epoch - warmup_steps) * np.pi / (max_epoch - warmup_steps))) * 0.5
            scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda=scheduler)
        else:
            scheduler = None

        if not load_model:
            model = pretrain(model, (train_loader, eval_loader), optimizer, max_epoch, device, scheduler,
                             args.coarse_layer, args.mask_edge, args.recover_rate, logger)
            model = model.cpu()

        if load_model:
            logging.info("Loading Model ... ")
            model.load_state_dict(torch.load("checkpoint.pt"))
        if save_model:
            logging.info("Saving Model ...")
            torch.save(model.state_dict(), "checkpoint.pt")

        model = model.to(device)
        model.eval()
        test_f1 = graph_classification_evaluation(model, pooler, eval_loader, device, args.coarse_layer)
        acc_list.append(test_f1)

    final_acc, final_acc_std = np.mean(acc_list), np.std(acc_list)
    print(f"# final_acc: {final_acc:.4f}±{final_acc_std:.4f}")
    print(f"# Total time: {(time.time() - start_time) / 60:.2f}min")
    save_result(args, final_acc, final_acc_std)


if __name__ == "__main__":
    args = build_args()
    if args.use_cfg:
        args = load_best_configs(args, "configs.yml")
    print(args)
    main(args)
