# coding: utf-8

import json
import mxnet as mx
import gluonnlp as nlp
import io
from tmnt.bow_vae.bow_models import BowNTM
from tmnt.bow_vae.bow_doc_loader import collect_stream_as_sparse_matrix, DataIterLoader, BowDataSet, file_to_sp_vec
from tmnt.preprocess.tokenizer import BasicTokenizer
from multiprocessing import Pool

class BowNTMInference(object):

    def __init__(self, param_file, specs_file, vocab_file, ctx=mx.cpu()):
        self.max_batch_size = 2
        with open(specs_file) as f:
            specs = json.loads(f.read())
        with open(vocab_file) as f:
            voc_js = f.read()
        self.vocab = nlp.Vocab.from_json(voc_js)
        self.ctx = ctx
        self.n_latent = specs['n_latent']
        enc_dim = specs['enc_hidden_dim']
        lat_distrib = specs['latent_distribution']
        emb_size = specs['embedding_size']
        self.model = BowNTM(self.vocab, enc_dim, self.n_latent, emb_size, latent_distrib=lat_distrib, ctx=ctx)
        self.model.load_parameters(str(param_file), allow_missing=False)


    def get_model_details(self, sp_vec_file):
        data_csr, _, labels = file_to_sp_vec(sp_vec_file, len(self.vocab))        
        ## 1) K x W matrix of P(term|topic) probabilities
        w = self.model.decoder.collect_params().get('weight').data().transpose() ## (K x W)
        w_pr = mx.nd.softmax(w, axis=1)
        ## 2) D x K matrix over the test data of topic probabilities
        dt_matrix = self.encode_csr(data_csr, use_probs=True)
        ## 3) D-length vector of document sizes
        doc_lengths = mx.nd.sum(data_csr, axis=1)
        ## 4) vocab (in same order as W columns)
        ## 5) frequency of each word w_i \in W over the test corpus
        term_cnts = mx.nd.sum(data_csr, axis=0)
        return w_pr, dt_matrix, doc_lengths, term_cnts


    def get_pyldavis_details(self, sp_vec_file):
        w_pr, dt_matrix, doc_lengths, term_cnts = self.get_model_details(sp_vec_file)
        d1 = w_pr.asnumpy().tolist()
        d2 = list(map(lambda x: x.asnumpy().tolist(), dt_matrix))
        d3 = doc_lengths.asnumpy().tolist()
        d5 = term_cnts.asnumpy().tolist()
        d4 = list(map(lambda i: self.vocab.idx_to_token[i], range(len(self.vocab.idx_to_token))))
        d = {'topic_term_dists': d1, 'doc_topic_dists': d2, 'doc_lengths': d3, 'vocab': d4, 'term_frequency': d5 }
        return d
    

    def export_full_model_inference_details(self, sp_vec_file, ofile):
        d = self.get_pyldavis_details(sp_vec_file)
        with io.open(ofile, 'w') as fp:
            json.dump(d, fp, sort_keys=True, indent=4)        


    def encode_texts(self, intexts):
        """
        intexts - should be a list of lists of tokens (each token list being a document)
        """
        in_strms = [nlp.data.SimpleDataStream([t]) for t in intexts]
        strm = nlp.data.SimpleDataStream(in_strms)
        return self.encode_text_stream(strm)

    def encode_vec_file(self, sp_vec_file):
        data_csr, _, labels = file_to_sp_vec(sp_vec_file, len(self.vocab))
        return self.encode_csr(data_csr), labels

    def encode_text_stream(self, strm):
        csr, _, _ = collect_stream_as_sparse_matrix(strm, pre_vocab=self.vocab)
        return self.encode_csr(csr)

    def encode_csr(self, csr, use_probs=False):
        batch_size = min(csr.shape[0], self.max_batch_size)
        last_batch_size = csr.shape[0] % batch_size        
        infer_iter = DataIterLoader(mx.io.NDArrayIter(csr[:-last_batch_size], None, batch_size, last_batch_handle='discard', shuffle=False))
        encodings = []
        for _, (data,_) in enumerate(infer_iter):
            data = data.as_in_context(self.ctx)
            encs = self.model.encode_data(data)
            if use_probs:
                #norm = mx.nd.norm(encs, axis=1, keepdims=True)
                e1 = encs - mx.nd.min(encs, axis=1).expand_dims(1)
                encs = mx.nd.softmax(e1 ** 0.5)
            encodings.extend(encs)
        ## handle the last batch explicitly as NDArrayIter doesn't do that for us
        if last_batch_size > 0:
            data = csr[-last_batch_size:].as_in_context(self.ctx)
            encs = self.model.encode_data(data)
            if use_probs:
                #norm = mx.nd.norm(encs, axis=1, keepdims=True)
                e1 = encs - mx.nd.min(encs, axis=1).expand_dims(1)
                encs = mx.nd.softmax(e1 ** 0.5)
            encodings.extend(encs)
        return encodings

    def get_top_k_words_per_topic(self, k):
        w = self.model.decoder.collect_params().get('weight').data()
        sorted_ids = w.argsort(axis=0, is_ascend=False)
        topic_terms = []
        for t in range(self.n_latent):
            top_k = [ self.vocab.idx_to_token[int(i)] for i in list(sorted_ids[:k, t].asnumpy()) ]
            topic_terms.append(top_k)
        return topic_terms

    def _test_inference_on_directory(self, directory, file_pattern=None):
        """
        Temporary test method to demonstrate use of inference on a set of files in a directory
        """
        pat = '*.txt' if file_pattern is None else file_pattern
        dataset_strm = BowDataSet(directory, pat, sampler='sequential') # preserve file ordering
        return self.encode_text_stream(dataset_strm)
        

class TextEncoder(object):

    """
    Takes a batch of text strings/documents and returns a matrix of their encodings (each row in the matrix
    corresponds to the encoding of the corresponding input text).

    Parameters
    ----------
    inference - the inference object using the trained model
    use_probs - boolean that indicates whether raw topic scores should be converted to probabilities or not (default = True)
    pool_size - integer that specifies the number of processes to use for concurrent text pre-processing
    """
    def __init__(self, inference, use_probs=True):
        self.inference = inference
        self.use_probs = use_probs
        self.vocab_len = len(self.inference.vocab.idx_to_token)
        self.tokenizer = BasicTokenizer(do_lower_case=True, use_stop_words=False)

    def _txt_to_vec(self, txt):
        toks = self.tokenizer.tokenize(txt)
        ids = [self.inference.vocab[token] for token in toks if token in self.inference.vocab]
        return ids

    def encode_single_string(self, txt):
        data = mx.nd.zeros((1, self.vocab_len))
        for t in self._txt_to_vec(txt):
            data[0][t] += 1.0
        encs = self.inference.model.encode_data(data)
        if self.use_probs:
            e1 = encs - mx.nd.min(encs, axis=1).expand_dims(1)
            encs = mx.nd.softmax(e1 ** 0.5)
        return encs

    def encode_batch(self, txts, pool_size=4):
        pool = Pool(pool_size)
        ids  = pool.map(self._txt_to_vec, txts)
        # Prevent zombie threads from hanging around
        pool.terminate()
        pool = None
        data = mx.nd.zeros((len(txts), self.vocab_len))
        for i, txt_ids in enumerate(ids):
            for t in txt_ids:
                data[i][t] += 1.0
        encs = self.inference.model.encode_data(data)
        if self.use_probs:
            e1 = encs - mx.nd.min(encs, axis=1).expand_dims(1)
            encs = mx.nd.softmax(e1 ** 0.5)
        return encs
