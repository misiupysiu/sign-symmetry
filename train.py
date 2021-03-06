"""
Modified from https://github.com/pytorch/examples/blob/master/imagenet/main.py
    - Implemented separate learning rates and optimizers for last (fc) layer vs. all other layers
    - Added/modified command line arguments
        - --algo
        - --last-layer-algo
        - --batch-manhattan
        - --last-layer-batch-manhattan
        - --no-sign-change
        - --last-layer-no-sign-change
        - --learning-rate
        - --last-layer-learning-rate
        - --lr-decay
        - --save-every-epoch
        - --save-every-n-epochs
Reference for training settings:
    - https://github.com/pytorch/examples/tree/master/imagenet
"""

import argparse
import os
import random
import shutil
import time
import warnings

import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.optim
import torch.utils.data
import torch.utils.data.distributed
import torchvision.transforms as transforms
import torchvision.datasets as datasets

from optim.bm_nsc_sgd import BMNSC_SGD

parser = argparse.ArgumentParser(description='PyTorch ImageNet Training')
parser.add_argument('--algo', default='sign_symmetry', type=str, metavar='ALGO',
                    help='algorithm for asymmetric feedback weight; ' +
                         'options: sign_symmetry, feedback_alignment, ' +
                         'sham, feedback_alignment_signed_init, ' +
                         'sign_symmetry_random_weights, or None; ' +
                         'None indicates to use unmodified torchvision reference model ' +
                         '(default: sign_symmetry)')
parser.add_argument('--last-layer-algo', '--lalgo', default='None', type=str, metavar='ALGO',
                    help='algorithm for asymmetric feedback weight for last layer; ' +
                         'None indicates to use unmodified torch.nn module ' +
                         'disabled if --algo is None (default: None)')
parser.add_argument('--batch-manhattan', '--bm', dest='batch_manhattan',
                    action='store_true', help='use batch manhattan for non-last layers')
parser.add_argument('--last-layer-batch-manhattan', '--lbm', dest='last_layer_batch_manhattan',
                    action='store_true', help='use batch manhattan for last layer')
parser.add_argument('--no-sign-change', '--nsc', dest='no_sign_change',
                    action='store_true', help='use no sign-change for non-last layers')
parser.add_argument('--last-layer-no-sign-change', '--lnsc', dest='last_layer_no_sign_change',
                    action='store_true', help='use no sign-change for last layer')
parser.add_argument('--lr', '--learning-rate', default=0.1, type=float,
                    metavar='LR', help='initial learning rate')
parser.add_argument('--llr', '--last-layer-learning-rate', default=0.1, type=float,
                    metavar='LLR', help='initial learning rate')
parser.add_argument('--lr-decay', '--lrd', default=10, type=int, metavar='LRD',
                    help='number of epochs after which lr is decreased 10x (default: 10)')
parser.add_argument('--save-every-epoch', '--see', dest='save_every_epoch',
                    action='store_true', help='save every epoch ' +
                    '(each to a unique name to prevent overwriting)')
parser.add_argument('--save-every-n-epochs', '--sene', default=-1, type=int, metavar='EPOCH',
                    help='if set and > 0, saves every n epochs '
                    '(each to a unique name to prevent overwriting)')
parser.add_argument('--prefix', metavar='DIR', default='.',
                    help='path to save files')

parser.add_argument('data', metavar='DIR',
                    help='path to dataset')
parser.add_argument('--arch', '-a', metavar='ARCH', default='resnet18')
parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                    help='number of data loading workers (default: 4)')
parser.add_argument('--epochs', default=90, type=int, metavar='N',
                    help='number of total epochs to run')
parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
                    help='manual epoch number (useful on restarts)')
parser.add_argument('-b', '--batch-size', default=256, type=int,
                    metavar='N', help='mini-batch size (default: 256)')
parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                    help='momentum')
parser.add_argument('--weight-decay', '--wd', default=1e-4, type=float,
                    metavar='W', help='weight decay (default: 1e-4)')
parser.add_argument('--print-freq', '-p', default=10, type=int,
                    metavar='N', help='print frequency (default: 10)')
parser.add_argument('--resume', default='', type=str, metavar='PATH',
                    help='path to latest checkpoint (default: none)')
parser.add_argument('-e', '--evaluate', dest='evaluate', action='store_true',
                    help='evaluate model on validation set')
parser.add_argument('--pretrained', dest='pretrained', action='store_true',
                    help='use pre-trained model')
parser.add_argument('--world-size', default=1, type=int,
                    help='number of distributed processes')
parser.add_argument('--dist-url', default='tcp://224.66.41.62:23456', type=str,
                    help='url used to set up distributed training')
parser.add_argument('--dist-backend', default='gloo', type=str,
                    help='distributed backend')
parser.add_argument('--seed', default=None, type=int,
                    help='seed for initializing training. ')
parser.add_argument('--gpu', default=None, type=int,
                    help='GPU id to use.')
best_prec1 = 0


def main():
    global args, best_prec1, lr_decay
    args = parser.parse_args()
    lr_decay = args.lr_decay

    os.makedirs(args.prefix, exist_ok=True)
    if args.algo == 'None':
        import pytorch_models as models
    else:
        import models

    if args.seed is not None:
        random.seed(args.seed)
        torch.manual_seed(args.seed)
        cudnn.deterministic = True
        warnings.warn('You have chosen to seed training. '
                      'This will turn on the CUDNN deterministic setting, '
                      'which can slow down your training considerably! '
                      'You may see unexpected behavior when restarting '
                      'from checkpoints.')

    if args.gpu is not None:
        warnings.warn('You have chosen a specific GPU. This will completely '
                      'disable data parallelism.')

    args.distributed = args.world_size > 1
    if args.distributed:
        dist.init_process_group(backend=args.dist_backend, init_method=args.dist_url,
                                world_size=args.world_size)

    # create model
    if args.algo == 'None':
        if args.pretrained:
            print("=> using pre-trained reference model '{}'".format(args.arch))
            model = models.__dict__[args.arch](pretrained=True)
        else:
            print("=> creating reference model '{}'".format(args.arch))
            model = models.__dict__[args.arch]()
    else:
        assert args.arch.startswith('resnet') or args.arch.startswith('alexnet'),\
            'only resnets or alexnet supported'
        if args.pretrained:
            raise ValueError(
                "Using non-standard models but pretrained set to True")
        print("=> creating asymmetric feedback model '{}' ".format(args.arch) +
              "with non-last layer af_algo '{}' and last layer af_algo '{}'".
              format(args.algo, args.last_layer_algo))
        model = models.__dict__[args.arch](
            af_algo=args.algo, last_layer_af_algo=args.last_layer_algo
        )

    if args.gpu is not None:
        model = model.cuda(args.gpu)
    elif args.distributed:
        model.cuda()
        model = torch.nn.parallel.DistributedDataParallel(model)
    else:
        if args.arch.startswith('alexnet') or args.arch.startswith('vgg'):
            model.features = torch.nn.DataParallel(model.features)
            model.cuda()
        else:
            model = torch.nn.DataParallel(model).cuda()

    if args.gpu is not None:
        if args.arch.startswith('resnet'):
            model_last_named_parameters = list(model.fc.named_parameters())
        elif args.arch.startswith('alexnet'):
            model_last_named_parameters = list(
                model.classifier[-1].named_parameters())
    else:
        if args.arch.startswith('resnet'):
            model_last_named_parameters = list(
                model.module.fc.named_parameters())
        elif args.arch.startswith('alexnet'):
            if args.distributed:
                model_last_named_parameters = list(
                    model.module.classifier[-1].named_parameters())
            else:
                model_last_named_parameters = list(
                    model.classifier[-1].named_parameters())

    model_last_parameters = [nparam[1]
                             for nparam in model_last_named_parameters]
    model_nonlast_named_parameters = \
        [nparam for nparam in model.named_parameters()
         if not any([nparam[1] is p_last for p_last in model_last_parameters])]
    model_nonlast_parameters = [nparam[1]
                                for nparam in model_nonlast_named_parameters]

    # define loss function (criterion) and optimizer
    criterion = nn.CrossEntropyLoss().cuda(args.gpu)
    param_groups = []
    lrs = []
    for use_bm, use_nsc, params, named_params, lr, label in zip(
            (args.batch_manhattan, args.last_layer_batch_manhattan),
            (args.no_sign_change, args.last_layer_no_sign_change),
            (model_nonlast_parameters, model_last_parameters),
            (model_nonlast_named_parameters, model_last_named_parameters),
            (args.lr, args.llr),
            ('non-last', 'last')
    ):
        print('%s layer(s): lr = %.0e%s%s' %
              (label, lr, ('', ', using Batch Manhattan')[use_bm],
               ('', ', using No-sign-change (bias excluded)')[use_nsc]))
        if use_nsc:
            bias_params = []
            nonbias_params = []
            for nparam in named_params:
                if nparam[0].rfind('bias') == len(nparam[0]) - 4:
                    bias_params.append(nparam[1])
                else:
                    nonbias_params.append(nparam[1])
            paramss = [bias_params, nonbias_params]
            use_nscs = [False, True]
        else:
            paramss = [params]
            use_nscs = [False]

        for params_, use_nsc_ in zip(paramss, use_nscs):
            param_groups.append({
                'params': params_,
                'lr': lr,
                'batch_manhattan': use_bm,
                'no_sign_change': use_nsc_,
            })
            lrs.append(lr)
    optimizer = BMNSC_SGD(param_groups, momentum=args.momentum,
                          weight_decay=args.weight_decay)

    # optionally resume from a checkpoint
    if args.resume:
        resumefpath = os.path.join(args.prefix, args.resume)
        if os.path.isfile(resumefpath):
            print("=> loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(resumefpath)
            args.start_epoch = checkpoint['epoch']
            best_prec1 = checkpoint['best_prec1']
            model.load_state_dict(checkpoint['state_dict'])
            optimizer.load_state_dict(checkpoint['optimizer'])
            print("=> loaded checkpoint '{}' (epoch {})"
                  .format(args.resume, checkpoint['epoch']))
        else:
            raise IOError("=> no checkpoint found at '{}'".format(args.resume))

    cudnn.benchmark = True

    # Data loading code
    normalize = transforms.Normalize(
        mean=[0.485, 0.456, 0.406],
        std=[0.229, 0.224, 0.225]
    )
    if args.data == 'CIFAR':
        traindir = '/data/CIFAR/train'
        valdir = '/data/CIFAR/val'
        
        transform_train = transforms.Compose([
            transforms.RandomCrop(32, padding=4),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            transforms.Normalize((0.4914, 0.4822, 0.4465),
                                 (0.2023, 0.1994, 0.2010)),
        ])

        transform_test = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize((0.4914, 0.4822, 0.4465),
                                 (0.2023, 0.1994, 0.2010)),
        ])

        train_dataset = datasets.CIFAR10(
            traindir,
            train=True,
            transform=transform_train,
            download=True)
        test_dataset = datasets.CIFAR10(
            valdir,
            train=False,
            transform=transform_test,
            download=True
        )

    else:
        traindir = os.path.join(args.data, 'train')
        valdir = os.path.join(args.data, 'val')
        transform_train = transforms.Compose([
            transforms.RandomResizedCrop(224),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            normalize,
        ])
        transform_test = transforms.Compose([
            transforms.Resize(256),
            transforms.CenterCrop(224),
            transforms.ToTensor(),
            normalize,
        ])

        train_dataset = datasets.ImageFolder(
            traindir,
            transform_train
        )
        test_dataset = datasets.ImageFolder(
            valdir,
            transform_test
        )

    if args.distributed:
        train_sampler = torch.utils.data.distributed.DistributedSampler(
            train_dataset)
    else:
        train_sampler = None

    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=args.batch_size, shuffle=(
            train_sampler is None),
        num_workers=args.workers, pin_memory=True, sampler=train_sampler)

    val_loader = torch.utils.data.DataLoader(
        test_dataset,
        batch_size=args.batch_size, shuffle=False,
        num_workers=args.workers, pin_memory=True)

    if args.evaluate:
        validate(val_loader, model, criterion)
        return

    for epoch in range(args.start_epoch, args.epochs):
        if args.distributed:
            train_sampler.set_epoch(epoch)
        adjust_learning_rate(optimizer, epoch, lrs)

        # train for one epoch
        train(train_loader, model, criterion, optimizer, epoch)

        # evaluate on validation set
        prec1 = validate(val_loader, model, criterion)

        # remember best prec@1 and save checkpoint
        is_best = prec1 > best_prec1
        best_prec1 = max(prec1, best_prec1)
        save_dict = {
            'epoch': epoch + 1,
            'arch': args.arch,
            'state_dict': model.state_dict(),
            'best_prec1': best_prec1,
            'optimizer': optimizer.state_dict(),
        }
        save_checkpoint(save_dict, is_best, epoch)


def train(train_loader, model, criterion, optimizer, epoch):
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()

    # switch to train mode
    model.train()

    end = time.time()
    for i, (input, target) in enumerate(train_loader):
        # measure data loading time
        data_time.update(time.time() - end)

        if args.gpu is not None:
            input = input.cuda(args.gpu, non_blocking=True)
        target = target.cuda(args.gpu, non_blocking=True)

        # compute output
        output = model(input)
        loss = criterion(output, target)

        # measure accuracy and record loss
        prec1, prec5 = accuracy(output, target, topk=(1, 5))
        losses.update(loss.item(), input.size(0))
        top1.update(prec1[0], input.size(0))
        top5.update(prec5[0], input.size(0))

        # compute gradient and do SGD step
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            print('Epoch: [{0}][{1}/{2}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Prec@1 {top1.val:.3f} ({top1.avg:.3f})\t'
                  'Prec@5 {top5.val:.3f} ({top5.avg:.3f})'.format(
                      epoch, i, len(train_loader), batch_time=batch_time,
                      data_time=data_time, loss=losses, top1=top1, top5=top5))


def validate(val_loader, model, criterion):
    batch_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()

    # switch to evaluate mode
    model.eval()

    with torch.no_grad():
        end = time.time()
        for i, (input, target) in enumerate(val_loader):
            if args.gpu is not None:
                input = input.cuda(args.gpu, non_blocking=True)
            target = target.cuda(args.gpu, non_blocking=True)

            # compute output
            output = model(input)
            loss = criterion(output, target)

            # measure accuracy and record loss
            prec1, prec5 = accuracy(output, target, topk=(1, 5))
            losses.update(loss.item(), input.size(0))
            top1.update(prec1[0], input.size(0))
            top5.update(prec5[0], input.size(0))

            # measure elapsed time
            batch_time.update(time.time() - end)
            end = time.time()

            if i % args.print_freq == 0:
                print('Test: [{0}/{1}]\t'
                      'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                      'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                      'Prec@1 {top1.val:.3f} ({top1.avg:.3f})\t'
                      'Prec@5 {top5.val:.3f} ({top5.avg:.3f})'.format(
                          i, len(val_loader), batch_time=batch_time, loss=losses,
                          top1=top1, top5=top5))

        print(' * Prec@1 {top1.avg:.3f} Prec@5 {top5.avg:.3f}'
              .format(top1=top1, top5=top5))

    return top1.avg


def save_checkpoint(state, is_best, epoch, filename='checkpoint.pth.tar'):
    torch.save(state, os.path.join(args.prefix, filename))
    if is_best:
        shutil.copyfile(filename, os.path.join(
            args.prefix, 'model_best.pth.tar'))
    if args.save_every_epoch \
            or (args.save_every_n_epochs > 0 and epoch % args.save_every_n_epochs == 0):
        shutil.copyfile(filename, os.path.join(
            args.prefix, 'epoch%03d.pth.tar' % epoch))


class AverageMeter(object):
    """Computes and stores the average and current value"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def adjust_learning_rate(optimizer, epoch, lr0s):
    """Sets the learning rate to the initial LR decayed by 10 every 30 epochs"""
    for param_group, lr0 in zip(optimizer.param_groups, lr0s):
        lr = lr0 * (0.1 ** (epoch // lr_decay))
        param_group['lr'] = lr


def accuracy(output, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    with torch.no_grad():
        maxk = max(topk)
        batch_size = target.size(0)

        _, pred = output.topk(maxk, 1, True, True)
        pred = pred.t()
        correct = pred.eq(target.view(1, -1).expand_as(pred))

        res = []
        for k in topk:
            correct_k = correct[:k].view(-1).float().sum(0, keepdim=True)
            res.append(correct_k.mul_(100.0 / batch_size))
        return res


if __name__ == '__main__':
    main()
