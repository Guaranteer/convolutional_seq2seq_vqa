import sys

sys.path.append('..')
from dataloaders.data_feed_open import Batcher
from models.model_dual_att import Model
from tools import wups, utils
import time
import tensorflow as tf
import numpy as np
import os
import json
import pickle as pkl


def load_file(filename):
    with open(filename, 'rb') as f1:
        return pkl.load(f1)


class Trainer(object):
    def __init__(self, params, model):
        self.params = params
        self.model = model
        self.index2word = load_file(self.params['index2word'])

    def train(self):
        # training
        sess_config = tf.ConfigProto()
        sess_config.gpu_options.allow_growth = True
        sess = tf.Session(config=sess_config)
        # module initialization
        self.model.build_model()
        self.model_path = os.path.join(self.params['cache_dir'])
        if not os.path.exists(self.model_path):
            print('create path: ', self.model_path)
            os.makedirs(self.model_path)

        self.last_checkpoint = None

        # main procedure including training & testing
        self.train_batcher = Batcher(self.params, self.params['train_json'], 'train', self.params['epoch_reshuffle'])
        self.valid_batcher = Batcher(self.params, self.params['val_json'], 'val')
        self.test_batcher = Batcher(self.params, self.params['test_json'], 'test')
        self.model_saver = tf.train.Saver()

        print('Trainnning begins......')
        self._train(sess)
        # testing
        print('Evaluating best model in file', self.last_checkpoint, '...')
        if self.last_checkpoint is not None:
            self.model_saver.restore(sess, self.last_checkpoint)
            self._test(sess)
        else:
            print('ERROR: No checkpoint available!')
        sess.close()

    def _train(self, sess):
        # tensorflow initialization
        global_step = tf.get_variable('global_step', [], initializer=tf.constant_initializer(0), trainable=False)
        learning_rates = tf.train.exponential_decay(self.params['learning_rate'], global_step,
                                                    decay_steps=self.params['lr_decay_n_iters'],
                                                    decay_rate=self.params['lr_decay_rate'], staircase=True)
        optimizer = tf.train.AdamOptimizer(learning_rates)
        train_proc = optimizer.minimize(self.model.train_loss, global_step=global_step)
        # train_proc_rl = optimizer.minimize(self.model.loss_rl, global_step=global_step)

        # training
        init_proc = tf.global_variables_initializer()
        sess.run(init_proc)
        # self.model_saver.restore(sess, '../results/0817221611-2670')
        best_epoch_acc = 0
        best_epoch_id = 0

        print('****************************')
        print('Trainning datetime:', time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time())))
        print('Trainning params')
        print(self.params)
        utils.count_total_variables()
        print('****************************')

        all_epoch_time = 0
        epoch_time_list = list()
        for i_epoch in range(self.params['max_epoches']):
            # train an epoch

            all_batch_time = 0
            self.train_batcher.reset()
            i_batch = 0
            loss_sum = 0

            type_count = np.zeros(self.params['n_types'], dtype=float)
            wups_count = np.zeros(self.params['n_types'], dtype=float)
            wups_count2 = np.zeros(self.params['n_types'], dtype=float)
            bleu1_count = np.zeros(self.params['n_types'], dtype=float)

            for img_frame_vecs, img_frame_n, ques_vecs, ques_n, ques_word, ans_vecs, ans_n, ans_word, type_vec, batch_size in self.train_batcher.generate():
                if ans_vecs is None:
                    break

                batch_data = dict()
                batch_data[self.model.y] = ans_word
                batch_data[self.model.input_x] = img_frame_vecs
                batch_data[self.model.input_x_len] = img_frame_n
                batch_data[self.model.input_q] = ques_vecs
                batch_data[self.model.input_q_len] = ques_n
                batch_data[self.model.ans_vec] = ans_vecs
                batch_data[self.model.is_training] = True
                batch_data[self.model.batch_size] = batch_size

                mask_matrix = np.zeros([np.shape(ans_n)[0], self.params['max_n_a_words']], np.int32)
                for ind, row in enumerate(mask_matrix):
                    row[:ans_n[ind]] = 1
                batch_data[self.model.y_mask] = mask_matrix

                batch_t1 = time.time()
                _, train_loss, train_ans, learning_rate = sess.run(
                    [train_proc, self.model.train_loss, self.model.answer_word_train, learning_rates],
                    feed_dict=batch_data)
                # train_ans, learning_rate = sess.run([self.model.answer_word_train, learning_rates], feed_dict=batch_data)
                batch_t2 = time.time()
                batch_time = batch_t2 - batch_t1
                all_batch_time += batch_time

                # get word and calculate WUPS
                train_ans = np.transpose(np.array(train_ans), (1, 0))
                reward = np.ones(len(ans_vecs), dtype=float)
                for i in range(len(type_vec)):
                    type_count[type_vec[i]] += 1
                    ground_a = list()
                    for l in range(self.params['max_n_a_words']):
                        word = ans_word[i][l]
                        if self.index2word[word] == 'EOS':
                            break
                        ground_a.append(self.index2word[word])

                    generate_a = list()
                    for l in range(self.params['max_n_a_words']):
                        word = train_ans[i][l]
                        if self.index2word[word] == 'EOS':
                            break
                        generate_a.append(self.index2word[word])

                    question = list()
                    for l in range(self.params['max_n_q_words']):
                        word = ques_word[i][l]
                        if self.index2word[word] == '<PAD>':
                            break
                        question.append(self.index2word[word])

                    wups_value = wups.compute_wups(ground_a, generate_a, 0.0)
                    wups_value2 = wups.compute_wups(ground_a, generate_a, 0.9)
                    bleu1_value = wups.compute_wups(ground_a, generate_a, -1)
                    wups_count[type_vec[i]] += wups_value
                    wups_count2[type_vec[i]] += wups_value2
                    bleu1_count[type_vec[i]] += bleu1_value

                    reward[i] = bleu1_value

                # batch_data[self.model.reward] = reward
                # _, train_loss = sess.run([train_proc, self.model.train_loss], feed_dict=batch_data)

                # display batch info
                i_batch += 1
                loss_sum += train_loss
                if i_batch % self.params['display_batch_interval'] == 0:
                    print('Epoch %d, Batch %d, loss = %.4f, %.3f seconds/batch' % (
                    i_epoch, i_batch, train_loss, all_batch_time / i_batch))
                    # print('question:    ', question)
                    # print('ground_a:    ', ground_a)
                    # print('generated:    ', generate_a)
                    # print('wups_value:  ', wups_value)
                    # print('wups_value2: ', wups_value2)
                    # print('Bleu1 value: ', bleu1_value)

            print('****************************')
            wup_acc = wups_count.sum() / type_count.sum()
            wup_acc2 = wups_count2.sum() / type_count.sum()
            bleu1_acc = bleu1_count.sum() / type_count.sum()
            print('Overall Wup (@0):', wup_acc, '[', wups_count.sum(), '/', type_count.sum(), ']')
            print('Overall Wup (@0.9):', wup_acc2, '[', wups_count2.sum(), '/', type_count.sum(), ']')
            print('Overall Bleu1:', bleu1_acc, '[', bleu1_count.sum(), '/', type_count.sum(), ']')
            type_wup_acc = [wups_count[i] / type_count[i] for i in range(self.params['n_types'])]
            type_wup_acc2 = [wups_count2[i] / type_count[i] for i in range(self.params['n_types'])]
            type_bleu1_acc = [bleu1_count[i] / type_count[i] for i in range(self.params['n_types'])]
            print('Wup@0 for each type:', type_wup_acc)
            print('Wup@0.9 for each type:', type_wup_acc2)
            print('Bleu1 for each type:', type_bleu1_acc)
            print(type_count)

            # print info

            avg_batch_loss = loss_sum / i_batch
            all_epoch_time += all_batch_time
            epoch_time_list.append(all_epoch_time)
            print('Epoch %d ends. Average loss %.3f. %.3f seconds/epoch' % (i_epoch, avg_batch_loss, all_batch_time))
            print('learning_rate: ', learning_rate)

            if i_epoch % self.params['evaluate_interval'] == 0:
                print('****************************')
                print('Overall evaluation')
                print('****************************')
                _, valid_acc, _ = self._test(sess)
                print('****************************')
            else:
                print('****************************')
                print('Valid evaluation')
                print('****************************')
                valid_acc = self._evaluate(sess, self.model, self.valid_batcher)
                print('****************************')

            # save model and early stop
            if valid_acc > best_epoch_acc:
                best_epoch_acc = valid_acc
                best_epoch_id = i_epoch
                print('Saving new best model...')
                timestamp = time.strftime("%m%d%H%M%S", time.localtime())
                self.last_checkpoint = self.model_saver.save(sess, self.model_path + timestamp, global_step=global_step)
                print('Saved at', self.last_checkpoint)
            else:
                if i_epoch - best_epoch_id >= self.params['early_stopping']:
                    print('Early stopped. Best loss %.3f at epoch %d' % (best_epoch_acc, best_epoch_id))
                    break

    def _evaluate(self, sess, model, batcher):
        # evaluate the model in a set
        batcher.reset()
        type_count = np.zeros(self.params['n_types'], dtype=float)
        bleu1_count = np.zeros(self.params['n_types'], dtype=float)
        wups_count = np.zeros(self.params['n_types'], dtype=float)
        wups_count2 = np.zeros(self.params['n_types'], dtype=float)
        i_batch = 0
        all_batch_time = 0

        for img_frame_vecs, img_frame_n, ques_vecs, ques_n, ques_word, ans_vecs, ans_n, ans_word, type_vec, batch_size in batcher.generate():
            if ans_vecs is None:
                break

            batch_data = {
                model.input_q: ques_vecs,
                model.y: ans_word,
                model.input_x: img_frame_vecs,
                model.input_x_len: img_frame_n,
                model.input_q_len: ques_n,
                model.is_training: False,
                model.batch_size: batch_size,
                model.ans_vec: ans_vecs
            }

            mask_matrix = np.zeros([np.shape(ans_n)[0], self.params['max_n_a_words']], np.int32)
            for ind, row in enumerate(mask_matrix):
                row[:ans_n[ind]] = 1
            batch_data[model.y_mask] = mask_matrix

            batch_t1 = time.time()
            test_ans = sess.run(self.model.answer_word_test, feed_dict=batch_data)
            batch_t2 = time.time()
            batch_time = batch_t2 - batch_t1
            all_batch_time += batch_time
            test_ans = np.transpose(np.array(test_ans), (1, 0))

            for i in range(len(type_vec)):
                type_count[type_vec[i]] += 1
                ground_a = list()
                for l in range(self.params['max_n_a_words']):
                    word = ans_word[i][l]
                    if self.index2word[word] == 'EOS':
                        break
                    ground_a.append(self.index2word[word])

                generate_a = list()
                for l in range(self.params['max_n_a_words']):
                    word = test_ans[i][l]
                    if self.index2word[word] == 'EOS':
                        break
                    generate_a.append(self.index2word[word])

                question = list()
                for l in range(self.params['max_n_q_words']):
                    word = ques_word[i][l]
                    if self.index2word[word] == '<PAD>':
                        break
                    question.append(self.index2word[word])

                wups_value = wups.compute_wups(ground_a, generate_a, 0.0)
                wups_value2 = wups.compute_wups(ground_a, generate_a, 0.9)
                bleu1_value = wups.compute_wups(ground_a, generate_a, -1)
                # bleu1_value = bleu.calculate_bleu(' '.join(ground_a), ' '.join(generate_a))
                wups_count[type_vec[i]] += wups_value
                wups_count2[type_vec[i]] += wups_value2
                bleu1_count[type_vec[i]] += bleu1_value

            i_batch += 1
            if i_batch % 100 == 0:
                print('batch index:', i_batch)
                print('question:    ', question)
                print('ground_a:    ', ground_a)
                print('generated:    ', generate_a)

        wup_acc = wups_count.sum() / type_count.sum()
        wup_acc2 = wups_count2.sum() / type_count.sum()
        bleu1_acc = bleu1_count.sum() / type_count.sum()
        print('Overall Wup (@0):', wup_acc, '[', wups_count.sum(), '/', type_count.sum(), ']')
        print('Overall Wup (@0.9):', wup_acc2, '[', wups_count2.sum(), '/', type_count.sum(), ']')
        print('Overall Bleu1:', bleu1_acc, '[', bleu1_count.sum(), '/', type_count.sum(), ']')
        type_wup_acc = [wups_count[i] / type_count[i] for i in range(self.params['n_types'])]
        type_wup_acc2 = [wups_count2[i] / type_count[i] for i in range(self.params['n_types'])]
        type_bleu1_acc = [bleu1_count[i] / type_count[i] for i in range(self.params['n_types'])]
        print('Wup@0 for each type:', type_wup_acc)
        print('Wup@0.9 for each type:', type_wup_acc2)
        print('Bleu1 for each type:', type_bleu1_acc)
        print('type count:        ', type_count)
        print('all test time:  ', all_batch_time)
        return bleu1_acc

    def _test(self, sess):
        print('Validation set:')
        valid_acc = self._evaluate(sess, self.model, self.valid_batcher)
        print('Test set:')
        test_acc = self._evaluate(sess, self.model, self.test_batcher)
        return 0.0, valid_acc, test_acc


if __name__ == '__main__':
    config_file = '../configs/config_base.json'
    with open(config_file, 'r') as fr:
        config = json.load(fr)
    # print(config)
    model = Model(config)
    trainer = Trainer(config, model)
    trainer.train()
