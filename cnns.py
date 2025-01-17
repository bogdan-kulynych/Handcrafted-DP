import pathlib
import random
import argparse
import shutil
import os

import wandb
import numpy as np
import torch
import torch.nn as nn
from opacus import PrivacyEngine

from train_utils import get_device, train, test
from data import get_data, get_scatter_transform, get_scattered_loader
from models import CNNS, get_num_params
from dp_utils import ORDERS, get_privacy_spent, get_renyi_divergence, scatter_normalization


def save_checkpoint(state, is_best, filename="checkpoint.tar"):
    torch.save(state, filename)
    if is_best:
        shutil.copyfile(filename, f"{filename}_best")


def main(dataset, seed=0, augment=False, use_scattering=True, size=None,
         batch_size=2048, mini_batch_size=256, sample_batches=False,
         lr=1, optim="SGD", momentum=0.9, nesterov=False,
         noise_multiplier=1, max_grad_norm=0.1, epochs=100,
         input_norm=None, num_groups=None, bn_noise_multiplier=None,
         max_epsilon=None, out_dir="out", early_stop=True, device="cuda"):

    random.seed(seed)
    torch.manual_seed(seed)
    np.random.seed(seed)

    # Log run.
    run_name = f"cifar10_scatternet_{noise_multiplier}"
    run_dir = pathlib.Path(out_dir) / run_name
    if not run_dir.is_dir():
        run_dir.mkdir(parents=True)
    checkpoint_filename = run_dir / f"model_{seed}"
    run_params = {
        "project": "multiplicities",
        "job_type": "train",
        "group": run_name,
        "config": {
            "name": run_name,
            "out_path": checkpoint_filename,
            "dataset": "cifar10",
            "seed": seed,
            "sigma": noise_multiplier,
            "epochs": epochs,
            "batch_size": batch_size,
            "lr": lr,
        },
    }
    run = wandb.init(**run_params, name=f"model_{seed}")

    device = get_device(device)
    train_data, test_data = get_data(dataset, augment=augment)

    if use_scattering:
        scattering, K, _ = get_scatter_transform(dataset)
        scattering.to(device)
    else:
        scattering = None
        K = 3 if len(train_data.data.shape) == 4 else 1

    bs = batch_size
    assert bs % mini_batch_size == 0
    n_acc_steps = bs // mini_batch_size

    # Batch accumulation and data augmentation with Poisson sampling isn't implemented
    if sample_batches:
        assert n_acc_steps == 1
        assert not augment

    # def seed_worker(worker_id):
    #     worker_seed = worker_id
    #     # worker_seed = torch.initial_seed() % 2**32
    #     np.random.seed(worker_seed)
    #     random.seed(worker_seed)

    train_gen = torch.Generator()
    train_gen.manual_seed(0)
    test_gen = torch.Generator()
    test_gen.manual_seed(0)

    train_loader = torch.utils.data.DataLoader(
        train_data, batch_size=mini_batch_size, shuffle=True, num_workers=1, pin_memory=True,
        # worker_init_fn=seed_worker,
        generator=train_gen,
    )

    test_loader = torch.utils.data.DataLoader(
        test_data, batch_size=mini_batch_size, shuffle=False, num_workers=1, pin_memory=True,
        # worker_init_fn=seed_worker,
        generator=test_gen,
    )


    rdp_norm = 0
    if input_norm == "BN":
        # compute noisy data statistics or load from disk if pre-computed
        save_dir = f"bn_stats/{dataset}"
        os.makedirs(save_dir, exist_ok=True)
        bn_stats, rdp_norm = scatter_normalization(train_loader,
                                                   scattering,
                                                   K,
                                                   device,
                                                   len(train_data),
                                                   len(train_data),
                                                   noise_multiplier=bn_noise_multiplier,
                                                   orders=ORDERS,
                                                   save_dir=save_dir)
        model = CNNS[dataset](K, input_norm="BN", bn_stats=bn_stats, size=size)
    else:
        model = CNNS[dataset](K, input_norm=input_norm, num_groups=num_groups, size=size)

    model.to(device)

    if use_scattering and augment:
            model = nn.Sequential(scattering, model)
            train_loader = torch.utils.data.DataLoader(
                train_data, batch_size=mini_batch_size, shuffle=True,
                num_workers=1, pin_memory=True, drop_last=True,
                generator=train_gen)
    else:
        # pre-compute the scattering transform if necessery
        train_loader = get_scattered_loader(train_loader, scattering, device,
                                            drop_last=True, sample_batches=sample_batches,
                                            generator=train_gen)
        test_loader = get_scattered_loader(test_loader, scattering, device,
                                           generator=test_gen)

    print(f"model has {get_num_params(model)} parameters")

    if optim == "SGD":
        optimizer = torch.optim.SGD(model.parameters(), lr=lr,
                                    momentum=momentum,
                                    nesterov=nesterov)
    else:
        optimizer = torch.optim.Adam(model.parameters(), lr=lr)

    privacy_engine = PrivacyEngine(
        model,
        sample_rate=bs / len(train_data),
        alphas=ORDERS,
        noise_multiplier=noise_multiplier,
        max_grad_norm=max_grad_norm,
    )
    privacy_engine.attach(optimizer)

    best_acc = 0
    flat_count = 0

    for epoch in range(0, epochs):
        print(f"\nEpoch: {epoch}")

        train_loss, train_acc = train(model, train_loader, optimizer, n_acc_steps=n_acc_steps)
        test_loss, test_acc = test(model, test_loader)

        if noise_multiplier > 0:
            rdp_sgd = get_renyi_divergence(
                privacy_engine.sample_rate, privacy_engine.noise_multiplier
            ) * privacy_engine.steps
            epsilon, _ = get_privacy_spent(rdp_norm + rdp_sgd)
            epsilon2, _ = get_privacy_spent(rdp_sgd)
            print(f"ε = {epsilon:.3f} (sgd only: ε = {epsilon2:.3f})")

            if max_epsilon is not None and epsilon >= max_epsilon:
                return
        else:
            epsilon = None

        run.log(
            {
                "epoch": epoch,
                "train_loss": train_loss,
                "train_acc": train_acc,
                "test_loss": test_loss,
                "test_acc": test_acc,
                "epsilon": epsilon,
            }
        )

        # stop if we're not making progress
        if test_acc > best_acc:
            best_acc = test_acc
            flat_count = 0
        else:
            flat_count += 1
            if flat_count >= 20 and early_stop:
                print("plateau...")
                return

        save_checkpoint(
            {
                "epoch": epoch + 1,
                "model": "scatternet",
                "state_dict": model.state_dict(),
                "test_acc": test_acc,
                "best_acc": best_acc,
                "optimizer": optimizer.state_dict(),
            },
            is_best=True,
            filename=checkpoint_filename,
        )


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', choices=['cifar10', 'fmnist', 'mnist'])
    parser.add_argument('--seed', default=0, type=int)
    parser.add_argument('--size', default=None)
    parser.add_argument('--augment', action="store_true")
    parser.add_argument('--use_scattering', action="store_true")
    parser.add_argument('--batch_size', type=int, default=2048)
    parser.add_argument('--mini_batch_size', type=int, default=256)
    parser.add_argument('--lr', type=float, default=0.01)
    parser.add_argument('--optim', type=str, default="SGD", choices=["SGD", "Adam"])
    parser.add_argument('--momentum', type=float, default=0.9)
    parser.add_argument('--nesterov', action="store_true")
    parser.add_argument('--noise_multiplier', type=float, default=1)
    parser.add_argument('--max_grad_norm', type=float, default=0.1)
    parser.add_argument('--epochs', type=int, default=100)
    parser.add_argument('--input_norm', default=None, choices=["GroupNorm", "BN"])
    parser.add_argument('--num_groups', type=int, default=81)
    parser.add_argument('--bn_noise_multiplier', type=float, default=6)
    parser.add_argument('--max_epsilon', type=float, default=None)
    parser.add_argument('--early_stop', action='store_false')
    parser.add_argument('--sample_batches', action="store_true")
    parser.add_argument('--out_dir', default="out")
    parser.add_argument('--device', default="cuda")
    args = parser.parse_args()
    main(**vars(args))
