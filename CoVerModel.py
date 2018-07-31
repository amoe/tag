from tensorflowglove.tf_glove_copy import GloVeModel
from collections import defaultdict
import os
from random import shuffle
import tensorflow as tf

print('COVER MODEL')

class NotFitToCorpusError(Exception):
    pass

class CoVeRModel(GloVeModel):
    def __init__(self, embedding_size, context_size, max_vocab_size=100000,
                 min_occurrences=5, scaling_factor=3/4, cooccurrence_cap=100, batch_size=512,
                 learning_rate=0.05, num_epochs=50, log_dir=None, summary_batch_interval=1000, tsne_epoch_interval=None):
        self.embedding_size = embedding_size
        self.context_size = context_size
        self.max_vocab_size = max_vocab_size
        self.min_occurrences = min_occurrences
        self.scaling_factor = scaling_factor
        self.cooccurrence_cap = cooccurrence_cap
        self.batch_size = batch_size
        self.learning_rate = learning_rate
        self.num_epochs = num_epochs
        self.log_dir = log_dir
        self.summary_batch_interval = summary_batch_interval
        self.tsne_epoch_interval = tsne_epoch_interval
        self.covariance_embeddings = None
        self.__cooccurrence_tensor = None
        self.__vocab_size = 0
        self.k = 0

    def fit_corpora(self, corpora):
        print('FIT_CORPORA')
        self.__iter_corpora(corpora)
        self.__update_cooccurrence_tensor()
        self.__build_graph()

    def __iter_corpora(self, corpora):
        # iterate over corpora and stack cooccurrence matrix
        print('ITER_CORPORA')
        self.__cooccurrence_tensor = []
        self.models = []
        for corpus in corpora:
            model = GloVeModel(embedding_size=self.embedding_size,context_size=self.context_size,min_occurrences=self.min_occurrences,learning_rate=self.learning_rate,batch_size=self.batch_size)
            model._GloVeModel__fit_to_corpus(corpus, self.max_vocab_size, self.min_occurrences, model.left_context, model.right_context)
            self.models.append(model)
            self.__cooccurrence_tensor.append(model._GloVeModel__cooccurrence_matrix)
            self.__vocab_size += model.vocab_size
            self.k += 1

    def __update_cooccurrence_tensor(self):
        # update cooccurence tensor with 'k' added to the key
        print('UPDATE TENSOR')
        temp = self.__cooccurrence_tensor
        for i in range(len(temp)):
            dic = self.__cooccurrence_tensor[i]
            t = {}
            for key in dic.keys():
                new_key = key + (i,)
                t[new_key] = dic[key]
            temp[i] = t
        self.__cooccurrence_tensor = {k: v for d in temp for k, v in d.items()}

    def __build_graph(self):
        # build graph for training
        print('BUILD GRAPH')
        self.__graph = tf.Graph()
        with self.__graph.as_default(),self.__graph.device(_device_for_node):
            count_max = tf.constant([self.cooccurrence_cap], dtype=tf.float32,
                                     name='max_cooccurrence_count')
            scaling_factor = tf.constant([self.scaling_factor], dtype=tf.float32,
                                         name='scaling_factor')

            self.__focal_input = tf.placeholder(tf.int32, shape=[self.batch_size],
                                                name='focal_words')
            self.__context_input = tf.placeholder(tf.int32, shape=[self.batch_size],
                                                 name='context_words')
            self.__cooccurrence_count = tf.placeholder(tf.float32, shape=[self.batch_size],
                                                        name='cooccurrence_count')
            self.__covariance_input = tf.placeholder(tf.int32, shape=[self.batch_size],
                                                     name='covariance')

            focal_embeddings = tf.Variable(
                tf.random_uniform([self.__vocab_size, self.embedding_size], 1.0, -1.0),
                name='focal_embeddings')
            context_embeddings = tf.Variable(
                tf.random_uniform([self.__vocab_size, self.embedding_size], 1.0, -1.0),
                name='context_embeddings')
            covariance_embeddings = tf.Variable(
                tf.random_uniform([self.k, self.embedding_size], 1.0, -1.0),
                name='covariance_embeddings')

            focal_biases = tf.Variable(tf.random_uniform([self.__vocab_size, self.k], 1.0, -1.0),
                                       name='focal_biases')
            context_biases = tf.Variable(tf.random_uniform([self.__vocab_size, self.k], 1.0, -1.0),
                                         name='context_biases')

            focal_embedding = tf.nn.embedding_lookup([focal_embeddings], self.__focal_input)
            context_embedding = tf.nn.embedding_lookup([context_embeddings], self.__context_input)
            covariance_embedding = tf.nn.embedding_lookup([covariance_embeddings], self.__covariance_input)
            focal_bias = tf.gather_nd(focal_biases, tf.stack([self.__focal_input, self.__covariance_input], axis=1))
            context_bias = tf.gather_nd(context_biases, tf.stack([self.__context_input, self.__covariance_input], axis=1))

            weighting_factor = tf.minimum(
                1.0,
                tf.pow(
                    tf.div(self.__cooccurrence_count, count_max),
                    scaling_factor))

            foc_cov_product = tf.multiply(focal_embedding, covariance_embedding)
            con_cov_product = tf.multiply(context_embedding, covariance_embedding)
            embedding_product = tf.reduce_sum(tf.multiply(foc_cov_product, con_cov_product),1)

            log_cooccurrences = tf.log(tf.to_float(self.__cooccurrence_count))

            distance_expr = tf.square(tf.add_n([
                embedding_product,
                focal_bias,
                context_bias,
                tf.negative(log_cooccurrences)])) # ((ck*vi)' * (ck*vj) + bik + bjk - log(Aijk))^2

            single_losses = tf.multiply(weighting_factor, distance_expr)
            self.__total_loss = tf.reduce_sum(single_losses)
            tf.summary.scalar("GloVe_loss", self.__total_loss)
            self.__optimizer = tf.train.AdagradOptimizer(self.learning_rate).minimize(
                self.__total_loss)
            self.__summary = tf.summary.merge_all()

            self.__combined_embeddings = tf.add(focal_embeddings, context_embeddings,
                                                name='combined_embeddings')
            self.covariance_embeddings = tf.convert_to_tensor(covariance_embeddings)

    def train(self):
        print('TRAINING')
        should_write_summaries = self.log_dir is not None and self.summary_batch_interval
        should_generate_tsne = self.log_dir is not None and self.tsne_epoch_interval
        batches = self.__prepare_batches()
        total_steps = 0
        with tf.Session(graph=self.__graph) as session:
            if should_write_summaries:
                print('Writing TensorBoard summaries to {}'.format(self.log_dir))
                summary_writer = tf.summary.FileWriter(self.log_dir, graph=session.graph)
            tf.global_variables_initializer().run()
            for epoch in range(self.num_epochs):
                shuffle(batches)
                for batch_index, batch in enumerate(batches):
                    i_s, j_s, k_s, counts = batch
                    if len(counts) != self.batch_size:
                        continue
                #####
                    feed_dict = {
                        self.__focal_input: i_s,
                        self.__context_input: j_s,
                        self.__covariance_input: k_s,
                        self.__cooccurrence_count: counts} # the sequence//style of input to the session
                    session.run([self.__optimizer], feed_dict=feed_dict) # substitude the values in the geed_dict for the corresponding input values
                    if should_write_summaries and (total_steps + 1) % self.summary_batch_interval == 0:
                        summary_str = session.run(self.__summary, feed_dict=feed_dict)
                        summary_writer.add_summary(summary_str, total_steps)
                    total_steps += 1
                if should_generate_tsne and (epoch + 1) % self.tsne_epoch_interval == 0: # to visualize data on a graph
                    current_embeddings = self.__combined_embeddings.eval()
                    output_path = os.path.join(self.log_dir, "epoch{:03d}.png".format(epoch + 1))
                    self.generate_tsne(output_path, embeddings=current_embeddings)
                #####
            print('xxxxxxxEVALxxxxxxxxx')
            self.__embeddings = self.__combined_embeddings.eval() # combined_embeddings: addition of focal and context embeddings
            self.covariance_embeddings = self.covariance_embeddings.eval()
            if should_write_summaries:
                summary_writer.close()

    def __prepare_batches(self):
        print('PREPARE BATCHES')
        if self.__cooccurrence_tensor is None:
            raise NotFitToCorpusError(
                "Need to fit model to corpus before preparing training batches")
        cooccurrences = [(word_ids[0], word_ids[1], word_ids[2], count) 
                         for word_ids, count in self.__cooccurrence_tensor.items()]
        i_indices, j_indices, k_indices, counts = zip(*cooccurrences)
        return list(_batchify(self.batch_size, i_indices, j_indices, k_indices, counts))

    @property
    def covariates(self):
        if self.covariance_embeddings is None:
            raise NotTrainedError("Need to train model before accesing embeddings")
        return self.covariance_embeddings
    
    def get_glove_model(self, gmodel):
        print('COVER GET GLOVE MODEL')
        gmodel._GloVeModel__build_graph()
        gmodel.train(num_epochs=self.num_epochs, log_dir=self.log_dir, summary_batch_interval=self.summary_batch_interval, tsne_epoch_interval=self.tsne_epoch_interval)
        return gmodel
        
def _batchify(batch_size, *sequences):
    print('BATCHIFY')
    for i in range(0, len(sequences[0]), batch_size):
        yield tuple(sequence[i:i+batch_size] for sequence in sequences)

def _device_for_node(n):
    if n.type == "Matmul":
        return "/gpu:0"
    else:
        return"/cpu:0"