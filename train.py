import numpy as np
import matplotlib.pyplot as plt
import math
import seaborn as sns
import argparse
import tqdm
import random
import os
import logging

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import torchvision
from torch.optim.lr_scheduler import _LRScheduler
from torchvision import datasets, transforms
from torch.optim import AdamW, Adam
from torch.cuda.amp import autocast, GradScaler

from typing import Tuple
from collections import defaultdict

from einops import rearrange, repeat
from einops.layers.torch import Rearrange

from functools import partial

from utils.dataloader import datainfo, dataload
from model.vit import ViT
from utils.loss import LabelSmoothingCrossEntropy
from utils.scheduler import build_scheduler  
from utils.optimizer import get_adam_optimizer
from utils.utils import clip_gradients
from utils.utils import save_checkpoint, load_checkpoint


def get_args_parser():
    parser = argparse.ArgumentParser('SWIN ViT for CIFAR-10', add_help=False)
    parser.add_argument('--dir', type=str, default='./data',
                    help='Data directory')
    parser.add_argument('--num_classes', type=int, default=10, choices=[10, 100, 1000],
                    help='Dataset name')

    # Model parameters
    parser.add_argument('--patch_size', default=2, type=int, help="""Size in pixels of input square patches - default 4 (for 4x4 patches) """)
    parser.add_argument('--out_dim', default=1024, type=int, help="""Dimensionality of the SSL MLP head output. For complex and large datasets large values (like 65k) work well.""")

    parser.add_argument('--norm_last_layer', default=False, type=bool,
        help="""Whether or not to weight normalize the last layer of the MLP head.
        Not normalizing leads to better performance but can make the training unstable.
        In our experiments, we typically set this paramater to False with vit_small and True with vit_base.""")
    parser.add_argument('--use_bn_in_head', default=False, type=bool,
        help="Whether to use batch normalizations in projection head (Default: False)")

    parser.add_argument('--image_size', default=32, type=int, help=""" Size of input image. """)
    parser.add_argument('--in_channels',default=3, type=int, help=""" input image channels. """)
    parser.add_argument('--embed_dim',default=192, type=int, help=""" dimensions of vit """)
    parser.add_argument('--num_layers',default=9, type=int, help=""" No. of layers of ViT """)
    parser.add_argument('--num_heads',default=12, type=int, help=""" No. of heads in attention layer
                                                                                 in ViT """)
    parser.add_argument('--vit_mlp_ratio',default=2, type=int, help=""" MLP hidden dim """)
    parser.add_argument('--qkv_bias',default=True, type=bool, help=""" Bias in Q K and V values """)
    parser.add_argument('--drop_rate',default=0., type=float, help=""" dropout """)

    # Training/Optimization parameters
    parser.add_argument('--weight_decay', type=float, default=1e-1, help="""Initial value of the
        weight decay. With ViT, a smaller value at the beginning of training works well.""")
    parser.add_argument('--batch_size', default=128, type=int,
        help='Per-GPU batch-size : number of distinct images loaded on one GPU.')
    parser.add_argument('--epochs', default=100, type=int, help='Number of epochs of training.')
    parser.add_argument("--lr", default=0.001, type=float, help="""Learning rate at the end of
        linear warmup (highest LR used during training). The learning rate is linearly scaled
        with the batch size, and specified here for a reference batch size of 256.""")
    parser.add_argument("--warmup_epochs", default=10, type=int,
        help="Number of epochs for the linear learning-rate warm up.")
    parser.add_argument('--min_lr', type=float, default=1e-6, help="""Target LR at the
        end of optimization. We use a cosine LR schedule with linear warmup.""")
    parser.add_argument('--optimizer', default='adamw', type=str,
        choices=['adamw', 'sgd', 'lars'], help="""Type of optimizer. Recommend using adamw with ViTs.""")
    parser.add_argument('--drop_path_rate', type=float, default=0.1, help="stochastic depth rate")
    parser.add_argument('--label_smoothing', type=float, default=0.1,
                    help='Label smoothing for optimizer')
    parser.add_argument('--gamma', type=float, default=1.0,
                    help='Gamma value for Cosine LR schedule')

    # Misc
    parser.add_argument('--dataset', default='CIFAR10', type=str, choices=['CIFAR10', 'CIFAR100'], help='Please specify path to the training data.')
    parser.add_argument('--seed', default=42, type=int, help='Random seed.')
    parser.add_argument('--num_workers', default=8, type=int, help='Number of data loading workers per GPU.')
    parser.add_argument("--mlp_head_in", default=192, type=int, help="input dimension going inside MLP projection head")
    parser.add_argument('--checkpoint_dir', default=".", type=str, help='Path to save logs and checkpoints.')
    return parser



class Trainer:
    def __init__(self, model, train_loader, val_loader, optimizer, lr_scheduler, loss_fn, device, args):
        self.model = model
        self.train_loader = train_loader
        self.val_loader = val_loader
        self.optimizer = optimizer
        self.lr_scheduler = lr_scheduler
        self.loss_fn = loss_fn
        self.device = device
        self.args = args
        self.scaler = GradScaler()

        self.logger = logging.getLogger(__name__)
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    def train(self):
        train_losses, val_losses, train_accuracies, val_accuracies = [], [], [], []
        best_accuracy = 0.0
        
        print("\n--- GPU Information ---\n")
        
        if torch.cuda.is_available():
            print(f"Model is using device: {self.device}")
            print(f"CUDA Device: {torch.cuda.get_device_name(self.device)}")
            print(f"Total Memory: {torch.cuda.get_device_properties(self.device).total_memory / 1024 ** 2} MB")
        else:
            print("Model is using CPU")

        for epoch in range(self.args.epochs):
            epoch_progress_bar = tqdm(total=len(self.train_loader) + len(self.val_loader), desc=f"Epoch {epoch + 1}/{self.args.epochs}")
            
            print()
            print("\n--- Training Progress ---\n")

            # Training Phase
            self.model.train()
            total_train_loss, total_train_correct = 0.0, 0
            for images, labels in self.train_loader:
                images, labels = images.to(self.device), labels.to(self.device)
                self.optimizer.zero_grad()

                with autocast():
                    outputs = self.model(images)
                    loss = self.loss_fn(outputs, labels)

                self.scaler.scale(loss).backward()

                if self.args.clip_grad > 0:
                    self.scaler.unscale_(self.optimizer)
                    clip_gradients(self.model, self.args.clip_grad)

                self.scaler.step(self.optimizer)
                self.scaler.update()

                total_train_loss += loss.item()
                _, predicted = torch.max(outputs.data, 1)
                total_train_correct += (predicted == labels).sum().item()

                epoch_progress_bar.update(1)

            avg_train_loss = total_train_loss / len(self.train_loader)
            train_accuracy = total_train_correct / len(self.train_loader.dataset)

            # Validation Phase
            self.model.eval()
            total_val_loss, total_val_correct = 0.0, 0
            with torch.no_grad():
                for images, labels in self.val_loader:
                    images, labels = images.to(self.device), labels.to(self.device)
                    outputs = self.model(images)
                    loss = self.loss_fn(outputs, labels)
                    total_val_loss += loss.item()
                    _, predicted = torch.max(outputs.data, 1)
                    total_val_correct += (predicted == labels).sum().item()

                    epoch_progress_bar.update(1)

            avg_val_loss = total_val_loss / len(self.val_loader)
            val_accuracy = total_val_correct / len(self.val_loader.dataset)

            epoch_progress_bar.set_postfix({"Train Loss": avg_train_loss, "Train Acc": train_accuracy, "Val Loss": avg_val_loss, "Val Acc": val_accuracy})
            epoch_progress_bar.close()

            # Logging and Checkpointing
            self.logger.info(f"Epoch {epoch + 1}/{self.args.epochs}: Train Loss: {avg_train_loss:.4f}, Train Acc: {train_accuracy:.4f}, Val Loss: {avg_val_loss:.4f}, Val Acc: {val_accuracy:.4f}")
            if val_accuracy > best_accuracy:
                best_accuracy = val_accuracy
                save_checkpoint(self.model, self.optimizer, self.lr_scheduler, epoch, self.args.checkpoint_dir, best=True)
                self.logger.info(f"New best accuracy: {best_accuracy:.4f}, Model saved as 'best_model.pth'")

            self.lr_scheduler.step()

        return train_losses, val_losses, train_accuracies, val_accuracies


def main():
    args, unknown = get_args_parser().parse_known_args()

    torch.manual_seed(args.seed)
    torch.cuda.manual_seed(args.seed)
    np.random.seed(args.seed) 
    random.seed(args.seed) 
    torch.manual_seed(args.seed)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True

    device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")

    data_info = datainfo(args)
    normalize = [transforms.Normalize(mean=data_info['stat'][0], std=data_info['stat'][1])]

    print("\n--- Downloading Data ---\n")

    train_dataset, val_dataset = dataload(args, normalize, data_info)   

    train_loader = torch.utils.data.DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True,
                                                num_workers=args.num_workers, pin_memory=True)
    val_loader = torch.utils.data.DataLoader(val_dataset, batch_size=args.batch_size, shuffle=False, 
                                                num_workers=args.num_workers, pin_memory=True)
    
    print("\n--- Training and Validation Data Ready ---\n")

    model = ViT(img_size=[args.image_size],
            patch_size=args.patch_size,
            in_chans=args.in_channels,
            num_classes=0,
            embed_dim=192,
            depth=9,
            num_heads=12,
            mlp_ratio=2,
            qkv_bias=args.qkv_bias,
            drop_rate=args.drop_rate,
            drop_path_rate=args.drop_path_rate,
            norm_layer=partial(nn.LayerNorm, eps=1e-6)).to(device)

    loss = LabelSmoothingCrossEntropy()
    optimizer = get_adam_optimizer(model.parameters(), lr=args.lr, wd=args.weight_decay)
    lr_scheduler = build_scheduler(args, optimizer)
    
    Trainer(model, train_loader, val_loader, optimizer, lr_scheduler, loss, device, args).train()

if __name__ == "__main__":
    main()
