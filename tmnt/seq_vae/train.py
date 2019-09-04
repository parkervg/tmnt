# codeing: utf-8

import argparse, tarfile
import math
import os
import numpy as np
import logging
import json
import datetime
import io
import gluonnlp as nlp
import string
import re

import mxnet as mx
import mxnet.ndarray as F
from mxnet import gluon
from mxnet.gluon import nn
from mxnet import autograd as ag
from tmnt.seq_vae.trans_seq_models import BertTransVAE, PureTransformerVAE
from tmnt.utils.log_utils import logging_config
from tmnt.seq_vae.seq_data_loader import load_dataset_bert, load_dataset_basic


def get_bert_model(args, bert_base, ctx):
    model = BertTransVAE(bert_base, args.latent_dist, wd_embed_dim=args.wd_embed_dim, num_units=args.num_units, num_heads=args.num_heads,
                         transformer_layers=args.transformer_layers,
                         n_latent=args.latent_dim, max_sent_len=args.sent_size,
                         kappa = args.kappa, 
                         batch_size=args.batch_size,
                         kld=args.kld_wt, ctx=ctx)
    model.latent_dist.initialize(init=mx.init.Xavier(magnitude=2.34), ctx=ctx)
    model.decoder.initialize(init=mx.init.Xavier(magnitude=2.34), ctx=ctx)
    model.out_embedding.initialize(init=mx.init.Uniform(0.1), ctx=ctx)
    model.inv_embed.initialize(init=mx.init.Uniform(0.1), ctx=ctx)
    return model

def get_basic_model(args, vocab, ctx):
    model = PureTransformerVAE(vocab, args.latent_dist, num_units=args.num_units, num_heads=args.num_heads,
                               n_latent=args.latent_dim, max_sent_len=args.sent_size,
                               transformer_layers=args.transformer_layers,
                               kappa = args.kappa, 
                               batch_size=args.batch_size,
                               kld=args.kld_wt, ctx=ctx)
    model.latent_dist.initialize(init=mx.init.Xavier(magnitude=2.34), ctx=ctx)
    model.encoder.initialize(init=mx.init.Xavier(magnitude=2.34), ctx=ctx)
    model.decoder.initialize(init=mx.init.Xavier(magnitude=2.34), ctx=ctx)
    return model


def train_trans_vae(args, model, data_train, data_test=None, ctx=mx.cpu(), report_fn=None, use_bert=False):
    
    dataloader = mx.gluon.data.DataLoader(data_train, batch_size=args.batch_size,
                                           shuffle=True, last_batch='rollover')
    if data_test:
        dataloader_test = mx.gluon.data.DataLoader(data_test, batch_size=args.batch_size,
                                               shuffle=False) if data_test else None

    num_train_examples = len(data_train)
    num_train_steps = int(num_train_examples / args.batch_size * args.epochs)
    warmup_ratio = args.warmup_ratio
    num_warmup_steps = int(num_train_steps * warmup_ratio)
    step_num = 0
    differentiable_params = []

    gen_trainer = gluon.Trainer(model.collect_params(), args.optimizer,
                            {'learning_rate': args.gen_lr, 'epsilon': 1e-6, 'wd':args.weight_decay})

    # Do not apply weight decay on LayerNorm and bias terms
    for _, v in model.collect_params('.*beta|.*gamma|.*bias').items():
        v.wd_mult = 0.0

    lr = args.gen_lr
    
    for epoch_id in range(args.epochs):
        step_loss = 0
        step_recon_ls = 0
        step_kl_ls = 0
        for batch_id, seqs in enumerate(dataloader):
            step_num += 1
            if step_num < num_warmup_steps:
                new_lr = lr * step_num / num_warmup_steps
            else:
                offset = (step_num - num_warmup_steps) * lr / ((num_train_steps - num_warmup_steps) * args.offset_factor)
                new_lr = max(lr - offset, args.min_lr)
            gen_trainer.set_learning_rate(new_lr)
            with mx.autograd.record():
                if use_bert:
                    input_ids, valid_length, type_ids = seqs
                    ls, recon_ls, kl_ls, predictions = model(input_ids.as_in_context(ctx), type_ids.as_in_context(ctx),
                                valid_length.astype('float32').as_in_context(ctx))
                else:
                    input_ids = seqs
                    ls, recon_ls, kl_ls, predictions = model(input_ids.as_in_context(ctx))
                ls = ls.mean()
            ls.backward()
            gen_trainer.step(1) # step of 1 since we averaged loss over batch
            step_loss += ls.asscalar()
            step_recon_ls += recon_ls.mean().asscalar()
            step_kl_ls += kl_ls.mean().asscalar()
            if (batch_id + 1) % (args.log_interval) == 0:
                logging.info('[Epoch {}/{} Batch {}/{}] loss={:.4f}, recon_loss={:.4f}, kl_loss={:.4f}, gen_lr={:.7f}'
                             .format(epoch_id, args.epochs, batch_id + 1, len(dataloader),
                                     step_loss / args.log_interval, step_recon_ls / args.log_interval, step_kl_ls / args.log_interval,
                                     gen_trainer.learning_rate))
                step_loss = 0
                step_recon_ls = 0
                step_kl_ls = 0
            if (batch_id + 1) % args.log_interval == 0:
                if report_fn:
                    mx.nd.waitall()
                    report_fn(input_ids, predictions)


def get_report_reconstruct_data_fn(vocab, pad_id=0):
    def report_reconstruct_data_fn(data, predictions):
        reconstructed_sent_ids = mx.nd.argmax(predictions[0],1).asnumpy() ## get the first item of batch and arg_max over vocab size
        input_sent_ids = data[0].asnumpy()
        rec_sent = [vocab.idx_to_token[int(i)] for i in reconstructed_sent_ids if i != pad_id]   # remove <PAD> token from rendering
        in_sent = [vocab.idx_to_token[int(i)] for i in input_sent_ids if i != pad_id]
        in_ids = [str(i) for i in input_sent_ids]
        logging.info("---------- Reconstruction Output/Comparison --------")
        logging.info("Input Ids = {}".format(' '.join(in_ids)))
        logging.info("Input = {}".format(' '.join(in_sent)))
        logging.info("Reconstructed = {}".format(' '.join(rec_sent)))
    return report_reconstruct_data_fn
        

def train_main(args):
    i_dt = datetime.datetime.now()
    train_out_dir = '{}/train_{}_{}_{}_{}_{}_{}'.format(args.save_dir,i_dt.year,i_dt.month,i_dt.day,i_dt.hour,i_dt.minute,i_dt.second)
    print("Set logging config to {}".format(train_out_dir))
    logging_config(folder=train_out_dir, name='train_cvae', level=logging.INFO, no_console=False)
    logging.info(args)
    context = mx.cpu() if args.gpus is None or args.gpus == '' else mx.gpu(int(args.gpus))
    if args.use_bert:
        data_train, bert_base, vocab = load_dataset_bert(args.input_file, max_len=args.sent_size, ctx=context)
        model = get_bert_model(args, bert_base, context)
        report_fn = get_report_reconstruct_data_fn(vocab)
        train_trans_vae(args, model, data_train, data_test=None, ctx=context, report_fn=report_fn, use_bert=True)
    else:
        emb = nlp.embedding.create('glove', source = args.embedding_source) if args.embedding_source else None
        data_train, vocab = load_dataset_basic(args.input_file, vocab=None, max_len=args.sent_size,
                                               max_vocab_size=args.max_vocab_size, ctx=context)
        if emb:
            vocab.set_embedding(emb)
            _, emb_size = vocab.embedding.idx_to_vec.shape
            oov_items = 0
            for word in vocab.embedding._idx_to_token:
                if (vocab.embedding[word] == mx.nd.zeros(emb_size)).sum() == emb_size:
                    oov_items += 1
                    vocab.embedding[word] = mx.nd.random.normal(0.0, 0.1, emb_size)
            logging.info("** There are {} out of vocab items **".format(oov_items))
        else:
            logging.info("** No pre-trained embedding provided, learning embedding weights from scratch **")
        model = get_basic_model(args, vocab, context)
        report_fn = get_report_reconstruct_data_fn(vocab)
        train_trans_vae(args, model, data_train, data_test=None, ctx=context, report_fn=report_fn, use_bert=False)
        
