# encoding: utf-8

# author: BrikerMan
# contact: eliyar917@gmail.com
# blog: https://eliyar.biz

# file: model.py
# time: 2:34 下午

from typing import Any

import numpy as np
import tensorflow as tf
import tqdm

from kashgari.logger import logger
from kashgari.embeddings import BareEmbedding
from kashgari.embeddings.abc_embedding import ABCEmbedding
from kashgari.generators import CorpusGenerator, Seq2SeqDataSet
from kashgari.processors import SequenceProcessor
from kashgari.tasks.seq2seq.encoder import GRUEncoder
from kashgari.tasks.seq2seq.decoder import AttGRUDecoder
from kashgari.types import TextSamplesVar


class Seq2Seq:
    def __init__(self,
                 encoder_embedding: ABCEmbedding = None,
                 decoder_embedding: ABCEmbedding = None,
                 encoder_seq_length: int = None,
                 decoder_seq_length: int = None,
                 **kwargs: Any):
        """
        Init Labeling Model

        Args:
            embedding: embedding object
            sequence_length: target sequence length
            hyper_parameters: hyper_parameters to overwrite
            **kwargs:
        """
        logger.warning("This is a experimental API, it might changed in next version")
        if encoder_embedding is None:
            self.encoder_embedding = BareEmbedding(embedding_size=256)  # type: ignore
        else:
            self.encoder_embedding = encoder_embedding

        if decoder_embedding is None:
            self.decoder_embedding = BareEmbedding(embedding_size=256)  # type: ignore
        else:
            self.decoder_embedding = decoder_embedding

        self.encoder_processor = SequenceProcessor(min_count=1)
        self.decoder_processor = SequenceProcessor(build_vocab_from_labels=True, min_count=1)

        self.encoder: GRUEncoder = None
        self.decoder: AttGRUDecoder = None

        self.hidden_size: int = 1024

        self.encoder_seq_length = encoder_seq_length
        self.decoder_seq_length = decoder_seq_length

        self.optimizer = tf.keras.optimizers.Adam()
        self.loss_object = tf.keras.losses.SparseCategoricalCrossentropy(from_logits=True, reduction='none')

    def loss_function(self, real, pred):
        mask = tf.math.logical_not(tf.math.equal(real, 0))
        loss_ = self.loss_object(real, pred)

        mask = tf.cast(mask, dtype=loss_.dtype)
        loss_ *= mask

        return tf.reduce_mean(loss_)

    def build_model(self,
                    x_train: TextSamplesVar,
                    y_train: TextSamplesVar) -> None:
        train_gen = CorpusGenerator(x_train, y_train)
        self.build_model_generator(train_gen)

    def build_model_generator(self,
                              train_gen: CorpusGenerator) -> None:
        """
        Build model with a generator, This function will do:

        1. setup processor's vocab if the vocab is empty.
        2. calculate the sequence length if `sequence_length` is None.
        3. build up model architect.
        4. compile the ``tf_model`` with default loss, optimizer and metrics.

        Args:
            train_gen: train data generator

        """
        if self.encoder is None:
            self.encoder_processor.build_vocab_generator(train_gen)
            self.decoder_processor.build_vocab_generator(train_gen)
            self.encoder_embedding.setup_text_processor(self.encoder_processor)
            self.decoder_embedding.setup_text_processor(self.decoder_processor)

            self.encoder: GRUEncoder = GRUEncoder(self.encoder_embedding)
            self.decoder: AttGRUDecoder = AttGRUDecoder(self.decoder_embedding,
                                                        vocab_size=self.decoder_processor.vocab_size)

    @tf.function
    def train_step(self,
                   input_seq,
                   target_seq,
                   enc_hidden):
        loss = 0

        with tf.GradientTape() as tape:
            enc_output, enc_hidden = self.encoder(input_seq, enc_hidden)

            dec_hidden = enc_hidden

            bos_token_id = self.encoder_processor.vocab2idx[self.encoder_processor.token_bos]
            dec_input = tf.expand_dims([bos_token_id] * target_seq.shape[0], 1)

            # Teacher forcing - feeding the target as the next input
            for t in range(1, target_seq.shape[1]):
                # pass enc_output to the decoder
                predictions, dec_hidden, _ = self.decoder(dec_input, dec_hidden, enc_output)

                loss += self.loss_function(target_seq[:, t], predictions)

                # using teacher forcing
                dec_input = tf.expand_dims(target_seq[:, t], 1)

        batch_loss = (loss / int(target_seq.shape[1]))

        variables = self.encoder.trainable_variables + self.decoder.trainable_variables

        gradients = tape.gradient(loss, variables)

        self.optimizer.apply_gradients(zip(gradients, variables))

        return batch_loss

    def fit(self,
            x_train: TextSamplesVar,
            y_train: TextSamplesVar,
            batch_size: int = 64,
            epochs: int = 30):
        train_gen = CorpusGenerator(x_train, y_train)
        self.build_model_generator(train_gen)

        train_dataset = Seq2SeqDataSet(train_gen,
                                       batch_size=batch_size,
                                       encoder_processor=self.encoder_processor,
                                       encoder_seq_length=self.encoder_seq_length,
                                       decoder_processor=self.decoder_processor,
                                       decoder_seq_length=self.decoder_seq_length)

        train_gen.forever = False

        for epoch in range(epochs):
            enc_hidden = tf.zeros((batch_size, self.hidden_size))
            total_loss = []

            batch_progress_bar = tqdm.tqdm(train_dataset.take(),
                                           total=len(train_dataset),
                                           desc=f'Epoch: {epoch} Batch: ')

            for (inp, target) in batch_progress_bar:
                batch_loss = self.train_step(inp, target, enc_hidden)
                total_loss.append(batch_loss.numpy())
                info = f"Epoch {epoch + 1}/{epochs} | Loss : {np.mean(total_loss):.4f} " \
                       f"Batch Loss: {batch_loss.numpy():.4f}"
                batch_progress_bar.set_description_str(info)

    def predict(self,
                x_data: TextSamplesVar,
                max_len: int = 10,
                debug_info: bool = False):
        results = []
        attention_weights = []
        for sample in x_data:
            input_seq = self.encoder_processor.transform([sample], seq_length=self.encoder_seq_length)
            enc_hidden = tf.zeros((1, self.hidden_size))
            enc_output, enc_hidden = self.encoder(input_seq, enc_hidden)

            dec_hidden = enc_hidden

            token_out = []
            dec_input = tf.expand_dims(self.decoder_processor.vocab2idx[self.decoder_processor.token_bos], 0)

            for t in range(1, max_len):
                predictions, dec_hidden, att_weights = self.decoder(dec_input, dec_hidden, enc_output)
                attention_weights.append(tf.reshape(att_weights, (-1,)))
                next_tokens = tf.argmax(predictions[0]).numpy()
                if next_tokens == self.decoder_processor.vocab2idx[self.decoder_processor.token_eos]:
                    break
                token_out.append(next_tokens)
                dec_input = tf.expand_dims(token_out[-1:], 0)
            r = self.decoder_processor.inverse_transform([token_out])[0]
            if debug_info:
                print('\n---------------------------')
                print(f"input sentence  : {' '.join(sample)}")
                print(f"input idx       : {input_seq[0]}")
                print(f"output idx      : {token_out}")
                print(f"output sentence : {' '.join(r)}")
            results.append(r)
        return results, attention_weights


if __name__ == "__main__":
    import random
    from kashgari.corpus import ChineseDailyNerCorpus

    texts, labels = ChineseDailyNerCorpus.load_data('test')

    model = Seq2Seq(encoder_seq_lengths=50, decoder_seq_lengths=50)
    model.fit(texts, labels, epochs=1)

    x = random.sample(texts, 10)
    model.predict(x, max_len=20)

    for i in range(10):
        model.fit(texts, labels, epochs=10)
        x = random.sample(texts, 10)
        model.predict(x, max_len=20)
