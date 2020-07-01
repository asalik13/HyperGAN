import os
import sys
import time
import argparse
import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import autograd

import itertools
import pprint

import ops
import utils
import netdef
import datagen
import evaluate_uncertainty as uncertainty
from hypernetwork import hypernetwork
from stein_estimators import GradientEstimatorStein


def load_args():
    parser = argparse.ArgumentParser(description='HyperNetwork Arguments')
    parser.add_argument('-s', '--s_dim', default=512, type=int,
            help='encoder dimension')
    parser.add_argument('-z', '--z_dim', default=32, type=int,
            help='latent space width')
    parser.add_argument('--num_hidden_gen', default=2, type=int,
            help='g hidden dimension')
    parser.add_argument('--hidden_w_gen', default=32, type=int,
            help='g hidden dimension')
    parser.add_argument('-p', '--num_particles', default=32, type=int,
            help='')
    parser.add_argument('-t', '--target_arch', default='lenet', type=str,
            help='')
    parser.add_argument('-g', '--grad', default='svgd', type=str,
            help='')
    parser.add_argument('-r', '--resume', action='store_true',
            help='')
    parser.add_argument('-d', '--dataset', default='mnist', type=str,
            help='')
    parser.add_argument('--use_bias', action='store_true', default=True,
            help='')
    parser.add_argument('--use_mixer', action='store_true',
            help='')
    parser.add_argument('--pretrain_mixer', action='store_true',
            help='')
    parser.add_argument('--use_bn', action='store_true',
            help='')
    parser.add_argument('--test_ensemble', action='store_true',
            help='')
    parser.add_argument('--test_uncertainty', action='store_true',
            help='')
    parser.add_argument('--vote', default='hard', type=str,
            help='')

    args = parser.parse_args()
    return args


class HyperNetworkTrainer(object):
    def __init__(self, args):
        self.s = args.s_dim
        self.z = args.z_dim
        self.particles = args.num_particles
        self.target_arch = args.target_arch
        self.use_bn = args.use_bn
        self.use_bias = args.use_bias
        self.pretrain_mixer = args.pretrain_mixer
        self.dataset = args.dataset
        self.grad_type = args.grad
        
        self.epochs = 200
        self.vote = args.vote
        self.test_ensemble = args.test_ensemble
        self.test_uncertainty = args.test_uncertainty
        
        self.hidden_w_gen = args.hidden_w_gen
        self.num_hidden_gen = args.num_hidden_gen

        hidden_layers = [args.hidden_w_gen for _ in range(args.num_hidden_gen)]

        self.device = torch.device('cuda')
        torch.manual_seed(8734)        

        self.hypernetwork = hypernetwork.HyperNetwork(
                s_dim=args.s_dim,
                z_dim=args.z_dim, 
                use_mixer=args.use_mixer,
                noise_type='normal',
                hidden_layers=hidden_layers,
                particles=args.num_particles,
                use_bias=args.use_bias,
                use_batchnorm=args.use_bn,
                clear_bn_bias=True,
                activation=torch.nn.ReLU,
                last_layer_act=None,
                target_arch=args.target_arch,
                device=self.device)

        self.hypernetwork.set_target_architecture()
        self.hypernetwork.set_hypernetwork_mixer()
        self.hypernetwork.set_hypernetwork_layers()
        
        self.hypernetwork.print_architecture()
        self.hypernetwork.attach_optimizers(
                lr_mixer=5e-3,
                lr_generator=1e-4)

        self.grad_estimator = GradientEstimatorStein(
                particles=self.particles,
                data_loss_fn=F.cross_entropy,
                loss_grad_reduction=torch.sum,
                alpha=1.0,
                device=self.device,
                estimator=self.grad_type)

        if self.dataset == 'mnist':
            self.data_train, self.data_test = datagen.load_mnist()
            self.uncertainty_fn = uncertainty.eval_mnist_hypernetwork
            self.plot_fn = utils.plot_density_mnist
        elif self.dataset == 'cifar':
            self.data_train, self.data_test = datagen.load_cifar()
            self.uncertainty_fn = uncertainty.eval_cifar5_hypernetwork
            self.plot_fn = utils.plot_density_cifar

        self.best_test_acc = 0.
        self.best_test_loss = np.inf
        self.prefix = 'figures/hypernet/{}/{}hidden/{}'.format(
                self.grad_type,
                self.hidden_w_gen,
                self.dataset)

    def train(self):

        print ('==> Begin Training')
        for epoch in range(self.epochs):
            for batch_idx, (data, target) in enumerate(self.data_train):
                data, target = data.to(self.device), target.to(self.device)
                z = self.hypernetwork.sample_generator_input()
                theta = self.hypernetwork.sample_parameters(z)
                theta = theta.to(self.device)
                self.hypernetwork.set_parameters_to_model(theta)
                outputs = self.hypernetwork.forward_model(data)
                outputs = outputs.transpose(0, 1) # [B, N, D] -> [N, B, D]
                
                loss = self.grad_estimator.compute_gradients(
                        outputs,
                        theta,
                        target)
                self.hypernetwork.zero_grad()
                self.grad_estimator.apply_gradients()
                   
                self.hypernetwork.update_step()
                
                loss = loss.mean().item()
                
                """ Update Statistics """
                if batch_idx % 100 == 0:
                    utils.print_statistics_hypernetwork(
                            args.dataset,
                            epoch,
                            loss,
                            (self.best_test_acc, self.best_test_loss))
            
            if self.test_ensemble:
                ens_sizes = [1, 5, 10, self.particles]
            else:
                ens_sizes = [self.particles]
            self.evaluate_as_ensemble(ens_sizes, epoch)
                
            if self.test_uncertainty:
                for ens_size in [5, 10, self.particles]:
                    entropy_in, variance_in = self.uncertainty_fn(
                            self.hypernetwork,
                            ens_size,
                            self.s,
                            self.device,
                            outlier=False)
                    entropy_out, variance_out = self.uncertainty_fn(
                            self.hypernetwork,
                            ens_size,
                            self.s,
                            self.device,
                            outlier=True)
                    x_inliers = (entropy_in, variance_in)
                    x_outliers = (entropy_out, variance_out)
                    self.plot_fn(
                            x_inliers,
                            x_outliers,
                            ens_size,
                            self.prefix,
                            epoch+1)

    def evaluate_as_ensemble(self, ens_sizes, epoch):
        for n_models in ens_sizes:
            loss, acc, correct = self.test(n_models)
            print ('[Test Epoch {}]'.format(epoch+1))
            print ('[Ensemble Size: {}] Loss: {}, Accuracy: {}, ({}/{})'.format(
                n_models, loss, acc, correct, len(self.data_test.dataset)))

    def test(self, ens_size):
        test_acc = 0.
        test_loss = 0.
        correct = 0.
        
        self.hypernetwork.eval()
        for i, (data, target) in enumerate(self.data_test):
            data = data.to(self.device)
            target = target.to(self.device)
            z = self.hypernetwork.sample_generator_input()
            theta = self.hypernetwork.sample_parameters(z)
            self.hypernetwork.set_parameters_to_model(theta)
            outputs = self.hypernetwork.forward_model(data)
            outputs = outputs.transpose(0, 1) # [B, N, D] -> [N, B, D]
            outputs = outputs[:ens_size]
            losses = torch.stack([F.cross_entropy(x, target) for x in outputs])

            if self.vote == 'soft':
                probs = F.softmax(outputs, dim=-1)  # [ens, data, 10]
                preds = probs.mean(0)  # [data, 10]
                vote = preds.argmax(-1).cpu()  # [data, 1]

            elif self.vote == 'hard':
                probs = F.softmax(outputs, dim=-1) #[ens, data, 10]
                preds = probs.argmax(-1).cpu()  # [ens, data, 1]
                vote = preds.mode(0)[0]  # [data, 1]
            
            correct += vote.eq(target.cpu().data.view_as(vote)).float().cpu().sum()
            
            test_loss += losses.mean().item()
        test_loss /= len(self.data_test.dataset)
        test_acc = correct/len(self.data_test.dataset)
        self.hypernetwork.train()
        
        if test_loss < self.best_test_loss or test_acc > self.best_test_acc:
            print ('==> new best stats, saving')
            if test_loss < self.best_test_loss:
                self.best_test_loss = test_loss
            if test_acc > self.best_test_acc:
                self.best_test_acc = test_acc

        return test_loss, test_acc, correct


if __name__ == '__main__':

    args = load_args()
    trainer = HyperNetworkTrainer(args)
    trainer.train()

