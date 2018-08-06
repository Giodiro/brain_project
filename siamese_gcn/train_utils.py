import logging
import time
import os 

import numpy as np
import argparse

import torch
from torch.utils.data import DataLoader, Dataset
import torch.nn as nn
import torch.optim as optim

from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score, accuracy_score
from sklearn.preprocessing import normalize
from scipy.linalg import block_diag

from siamese_gcn.model import GraphConvNetwork, GraphConvNetwork_paper, GCN_multiple
from siamese_gcn.data_utils import build_onegraph_A, ToTorchDataset

import matplotlib.pyplot as plt

def training_step(gcn, data, optimizer, criterion, device):
    gcn.train()
    # get the inputs
    labels = data['Y']
    coh_array1 = data['f1']
    coh_array2 = data['f2']
    coh_array3 = data['f3']
    coh_array4 = data['f4']
    coh_array5 = data['f5']

    n, m = coh_array1.size()
    A1 = torch.zeros((n, 90, 90)).to(device)
    A2 = torch.zeros((n, 90, 90)).to(device)
    A3 = torch.zeros((n, 90, 90)).to(device)
    A4 = torch.zeros((n, 90, 90)).to(device)
    A5 = torch.zeros((n, 90, 90)).to(device)

    # we don't have feature so use identity for each graph
    X = torch.eye(90).expand(n, 90, 90)
    for i in range(n):
        A1[i] = torch.tensor(build_onegraph_A(coh_array1[i]))
        A2[i] = torch.tensor(build_onegraph_A(coh_array2[i]))
        A3[i] = torch.tensor(build_onegraph_A(coh_array3[i]))
        A4[i] = torch.tensor(build_onegraph_A(coh_array4[i]))
        A5[i] = torch.tensor(build_onegraph_A(coh_array5[i]))
        #print(A)     
    # zero the parameter gradients
    optimizer.zero_grad()
    # forward + backward + optimize
    outputs = gcn(X, A1, A2, A3, A4, A5)
    loss = criterion(outputs, labels)
    loss.backward()
    optimizer.step()
    _, predicted = torch.max(outputs.data, 1)
    predicted = predicted.numpy()
    labels=labels.numpy()
    pos_acc = accuracy_score(labels[labels==1], predicted[labels==1])
    neg_acc = accuracy_score(labels[labels==0], predicted[labels==0])
    bal = (pos_acc+neg_acc)/2
    return(loss.item(), bal)

def val_step(gcn, valloader, batch_size, device, criterion, logger):
    """ used for eval on validation set during training.
    """
    gcn.eval()
    correct = 0
    total = 0
    proba = []
    loss_val = 0
    y_true = np.asarray([])
    pred_val = np.asarray([])
    c = 0
    with torch.no_grad():
        for data in valloader:
            # get the inputs
            labels = data['Y']
            coh_array1 = data['f1']
            coh_array2 = data['f2']
            coh_array3 = data['f3']
            coh_array4 = data['f4']
            coh_array5 = data['f5']

            n, m = coh_array1.size()
            A1 = torch.zeros((n, 90, 90)).to(device)
            A2 = torch.zeros((n, 90, 90)).to(device)
            A3 = torch.zeros((n, 90, 90)).to(device)
            A4 = torch.zeros((n, 90, 90)).to(device)
            A5 = torch.zeros((n, 90, 90)).to(device)
            X = torch.eye(90).expand(n, 90, 90)
            for i in range(n):
                A1[i] = torch.tensor(build_onegraph_A(coh_array1[i]))
                A2[i] = torch.tensor(build_onegraph_A(coh_array2[i]))
                A3[i] = torch.tensor(build_onegraph_A(coh_array3[i]))
                A4[i] = torch.tensor(build_onegraph_A(coh_array4[i]))
                A5[i] = torch.tensor(build_onegraph_A(coh_array5[i]))
            y_true = np.append(y_true, labels)
            outputs_val = gcn(X, A1, A2, A3, A4, A5)
            proba.append(outputs_val.data.cpu().numpy())
            _, predicted = torch.max(outputs_val.data, 1)
            pred_val= np.append(pred_val, predicted.cpu().numpy())
            loss_val += criterion(outputs_val, labels).item()
            c += 1.0
        roc = roc_auc_score(y_true, np.concatenate(proba)[:,1])
        pos_acc = accuracy_score(y_true[y_true==1], pred_val[y_true==1])
        neg_acc = accuracy_score(y_true[y_true==0], pred_val[y_true==0])
        bal = (pos_acc+neg_acc)/2
        acc = accuracy_score(y_true, pred_val)
        logger.info('Val loss is: %.3f'%(loss_val/c))
        logger.info('Accuracy of the network val set : %.3f%% \n and ROC is %.3f' % (100*acc, roc))
        logger.info('Balanced accuracy of the network val set : %.3f%%' % (100*bal))
    return(loss_val/c, roc, acc, bal)

def training_loop(gcn, X_train, Y_train, batch_size, lr, device, logger, checkpoint_file, filename="", X_val=None, Y_val=None, nsteps=1000):
    train = ToTorchDataset(X_train, Y_train)
    if X_val is not None:
        val = ToTorchDataset(X_val, Y_val)
        valloader = torch.utils.data.DataLoader(val, batch_size=batch_size, shuffle=False, num_workers=4)
    # Creating the batches (balanced classes)
    torch.manual_seed(42)
    #sampler = torch.utils.data.sampler.WeightedRandomSampler(weight, len(X_train), replacement=True)
    #trainloader = torch.utils.data.DataLoader(train, batch_size=batch_size, sampler = sampler, num_workers=4)
    trainloader = torch.utils.data.DataLoader(train, batch_size=batch_size, shuffle=True, num_workers=4)
    # Define loss and optimizer
    n0 = np.sum([y==0 for y in Y_train])
    n1 = np.sum([y==1 for y in Y_train])
    n = n1+n0
    print(n0, n1)
    print([n/n0, n/n1])
    weight = torch.tensor([n/n0, n/n1], dtype=torch.float32)
    criterion = nn.CrossEntropyLoss(weight=weight) # applies softmax + cross entropy
    optimizer = optim.Adam(gcn.parameters(), lr=lr, weight_decay=5e-4)
    losses = []
    train_bal_acc = []
    loss_val = []
    acc_val = []
    roc_val = []
    step_val = []
    bal_acc_val = [] 
    current_batch_loss = 0
    current_batch_balacc = 0
    counter = 0
    # Training loop
    train_step=0
    while(train_step<nsteps):
        for data in trainloader:
            if(train_step<nsteps):
                current_loss, current_balacc = training_step(gcn, data, optimizer, criterion, device)
                train_step+=1
                current_batch_loss += current_loss
                current_batch_balacc += current_balacc
                losses += [current_loss]
                train_bal_acc += [current_balacc]
                counter += 1
            #print statistics in the epoch might be better - to do
            if (train_step%5==0) and (counter != 0):
                logger.debug("Mean of the training loss for step %d is %.3f" % (train_step, current_batch_loss/counter))
                logger.debug("Mean of the training balanced accuracy for step %d is %.3f%%" % (train_step, 100*current_batch_balacc/counter))
                current_batch_balacc = 0
                current_batch_loss = 0
                counter = 0
                if X_val is not None:
                    loss_val_e, roc_e, acc_e, bal_e = val_step(gcn, valloader, batch_size, device, criterion, logger)
                    loss_val.append(loss_val_e)
                    step_val.append(train_step)
                    roc_val.append(roc_e)
                    acc_val.append(acc_e)
                    bal_acc_val.append(bal_e)
    if X_val is not None:
        plt.clf()
        plt.plot(losses)
        plt.plot(step_val, loss_val)
        plt.savefig(checkpoint_file+"{}_loss.png".format(filename)) 
        plt.clf()
        plt.plot(train_bal_acc)
        plt.plot(step_val, bal_acc_val)
        plt.savefig(checkpoint_file+"{}_bal_acc.png".format(filename))
        """ if we want to save everything (I think useless)
        torch.save(gcn, checkpoint_file + filename +'.pt')
        losses_str = list(map(str, losses))
        with open(checkpoint_file + filename + '_train_losses.csv', 'w') as outfile:
            outfile.write("\n".join(losses_str))   
        if X_val is not None:
            str_loss_val = list(map(str, loss_val))
            roc_val = list(map(str, roc_val))
            acc_val = list(map(str, acc_val))
            # save the losses for plotting and monitor training
            with open(checkpoint_file + filename + '_lossval.csv', 'w') as outfile:
                outfile.write("\n".join(str_loss_val))
            with open(checkpoint_file+ filename + '_rocval.csv', 'w') as outfile:
                outfile.write("\n".join(roc_val))
            with open(checkpoint_file+ filename + '_accval.csv', 'w') as outfile:
                outfile.write("\n".join(acc_val))
        """

