"""
Training a covariate model
==========================

Another example with 20 news dataset. This involves
building a model using the labels as co-variates and
provides a simple test of the functionality. Co-variate
models, in general, should use co-variates that are 
somewhat orthogonal to the latent topics.
"""

from tmnt.estimator import BowEstimator, CovariateBowEstimator
import numpy as np
import gluonnlp as nlp
import os
from sklearn.datasets import fetch_20newsgroups
from tmnt.preprocess.vectorizer import TMNTVectorizer
from tmnt.inference import BowVAEInferencer
from tmnt.distribution import LogisticGaussianDistribution

n_samples = 2000
n_features = 1000

data, y = fetch_20newsgroups(shuffle=True, random_state=1,
                             remove=('headers', 'footers', 'quotes'),
                             return_X_y=True)
data_samples = data[:n_samples]
tf_vectorizer = TMNTVectorizer(vocab_size=1000)
X, _ = tf_vectorizer.fit_transform(data_samples)


num_covar_values = int(np.max(y)) + 1 # get the number of possible labels
m_estimator = CovariateBowEstimator(vocabulary=tf_vectorizer.get_vocab(), n_covars=num_covar_values)
                                    
_ = m_estimator.fit(X, y) # fit a covariate model using y
m_inferencer = BowVAEInferencer(m_estimator)

## the following returns a list of top 5 words per topic per covariate/label
t_terms = m_inferencer.get_top_k_words_per_topic_per_covariate(5)

## top-5 terms for each topic over label/covariate index = 4
cov_4_topics = t_terms[4]

