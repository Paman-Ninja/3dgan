from __future__ import absolute_import, division, print_function

import tensorflow as tf
from tensorflow.contrib.framework.python.ops.arg_scope import arg_scope
import math
import hem


# TODO Add summaries for all GPUs

class paper_sampler(hem.ModelPlugin):
    name = 'paper_sampler'

    @staticmethod
    def arguments():
        args = {
            '--g_lr': {
                'type': float,
                'default': 1e-3,
                'help': 'Learning rate for generator.' },
            '--d_lr': {
                'type': float,
                'default': 1e-3,
                'help': 'Learning rate for discriminator.' },
            '--g_beta1': {
                'type': float,
                'default': 0.9,
                'help': 'Beta1 for generator' },
            '--d_beta1': {
                'type': float,
                'default': 0.9,
                'help': 'Beta1 for discriminator.' },
            '--g_beta2': {
                'type': float,
                'default': 0.999,
                'help': 'Beta2 for generator.' },
            '--d_beta2': {
                'type': float,
                'default': 0.999,
                'help': 'Beta2 for discriminator.' },
            '--noise_layer': {
                'type': str,
                'choices': ['x', 'e1', 'e2', 'e3', 'e4', 'e4-512', 'd2', 'd3', 'd4'],
                'default': 'x',
                'help': 'Which node to add noise to. See generator code for the node names.'
                },
            '--e_bn': {
                'action': 'store_true',
                'default': 'false',
                'help': 'Use batchnorm in encoder.'
                }
            # '--': {
            #     'type':str,
            #     'default': 'baseline',
            #     'choices': ['baseline'], #, 'mean_adjusted', 'mean_provided'], #, 'mean_scene_provided'],
            #     'help': 'Which version of the model to run."' }
            }
        return args

    @hem.default_to_cpu
    def __init__(self, x_y, args):
        # init/setup
        g_opt = tf.train.AdamOptimizer(args.g_lr, args.g_beta1, args.g_beta2)
        d_opt = tf.train.AdamOptimizer(args.d_lr, args.d_beta1, args.d_beta2)
        g_tower_grads = []
        d_tower_grads = []
        global_step = tf.train.get_global_step()

        self.mean_image_placeholder = tf.placeholder(dtype=tf.float32, shape=(1, 29, 29))
        # self.var_image_placeholder = tf.placeholder(dtype=tf.float32, shape=(1, 29, 29))

        # foreach gpu...
        for x_y, scope, gpu_id in hem.tower_scope_range(x_y, args.n_gpus, args.batch_size):
            with tf.variable_scope('input_preprocess'):
                # split inputs and rescale
                x = tf.identity(x_y[0], name='tower_{}_x'.format(gpu_id))
                y = tf.identity(x_y[1], name='tower_{}_y'.format(gpu_id))

                # re-attach shape info
                x = tf.reshape(x, (args.batch_size, 3, 65, 65))
                # rescale from [0,1] to actual world depth
                y = y * 10.0
                y = hem.crop_to_bounding_box(y, 17, 17, 29, 29)
                # re-attach shape info
                y = tf.reshape(y, (args.batch_size, 1, 29, 29))
                y_bar = tf.reduce_mean(y, axis=[2, 3], keep_dims=True)
                y_bar = tf.identity(y_bar, name='tower_{}_y_bar'.format(gpu_id))
                x_sample = tf.stack([x[0]] * args.batch_size)
                y_sample = tf.stack([y[0]] * args.batch_size)
                x_sample = tf.identity(x_sample, name='tower_{}_x_sample'.format(gpu_id))
                y_sample = tf.identity(y_sample, name='tower_{}_y_sample'.format(gpu_id))

            # create model
            with tf.variable_scope('generator'):
                g = self.g_baseline(x, args, reuse=(gpu_id > 0))
                g_0 = tf.zeros_like(g)
                y_hat = g + y_bar
                y_0 = g_0 + y_bar
                g_sampler = self.g_baseline(x_sample, args, reuse=True)
                y_sample_bar = tf.reduce_mean(y_sample, axis=[2, 3], keep_dims=True)
                y_sampler = g_sampler + y_sample_bar
                g = tf.identity(g, 'tower_{}_g'.format(gpu_id))
                g_0 = tf.identity(g_0, 'tower_{}_g0'.format(gpu_id))
                y_hat = tf.identity(y_hat, 'tower_{}_y_hat'.format(gpu_id))
                y_0 = tf.identity(y_0, 'tower_{}_y0'.format(gpu_id))
                g_sampler = tf.identity(g_sampler, 'tower_{}_g_sampler'.format(gpu_id))
                y_sample_bar = tf.identity(y_sample_bar, 'tower_{}_y_sample_bar'.format(gpu_id))
                y_sampler = tf.identity(y_sampler, 'tower_{}_g_sampler'.format(gpu_id))

            with tf.variable_scope('discriminator'):
                # this is the 'mean_adjusted' model from paper_cgan.py
                d_fake, d_fake_logits = self.d_baseline(x, y_hat - y_bar, args, reuse=(gpu_id > 0))
                d_real, d_real_logits = self.d_baseline(x, y - y_bar, args, reuse=True)

                d_fake = tf.identity(d_fake, 'tower_{}_d_fake'.format(gpu_id))
                d_real = tf.identity(d_real, 'tower_{}_d_real'.format(gpu_id))
                d_fake_logits = tf.identity(d_fake_logits, 'tower_{}_d_fake_logits'.format(gpu_id))
                d_real_logits = tf.identity(d_real_logits, 'tower_{}_d_real_logits'.format(gpu_id))

            # calculate losses
            g_loss, d_loss = self.loss(d_real, d_real_logits, d_fake, d_fake_logits, reuse=(gpu_id > 0))
            # calculate gradients
            g_params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, 'generator')
            d_params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, 'discriminator')
            g_tower_grads.append(g_opt.compute_gradients(g_loss, var_list=g_params))
            d_tower_grads.append(d_opt.compute_gradients(d_loss, var_list=d_params))

        # average and apply gradients
        g_grads = hem.average_gradients(g_tower_grads, check_numerics=args.check_numerics)
        d_grads = hem.average_gradients(d_tower_grads, check_numerics=args.check_numerics)
        g_apply_grads = g_opt.apply_gradients(g_grads, global_step=global_step)
        d_apply_grads = d_opt.apply_gradients(d_grads, global_step=global_step)

        # add summaries
        hem.summarize_losses()
        hem.summarize_gradients(g_grads, name='g_gradients')
        hem.summarize_gradients(d_grads, name='d_gradients')
        generator_layers = [l for l in tf.get_collection('conv_layers') if 'generator' in l.name]
        discriminator_layers = [l for l in tf.get_collection('conv_layers') if 'discriminator' in l.name]
        hem.summarize_layers('g_activations', generator_layers, montage=True)
        hem.summarize_layers('d_activations', discriminator_layers, montage=True)
        self.montage_summaries(x, y, g, y_hat, args, name='y_hat')
        self.metric_summaries(x, y, g, y_hat, args, name='y_hat')
        self.metric_summaries(x, y, g_0, y_0, args, name='y_0')
        self.metric_summaries(x, y, g, tf.stack([self.mean_image_placeholder * 10.0] * args.batch_size), args, name='y_mean')
        self.metric_summaries(x_sample, y_sample, g_sampler, y_sampler, args, name='y_sampler')
        self.montage_summaries(x_sample, y_sample, g_sampler, y_sampler, args, name='y_sampler')

        # training ops
        self.g_train_op = g_apply_grads
        self.d_train_op = d_apply_grads
        self.all_losses = hem.collection_to_dict(tf.get_collection('losses'))

    def train(self, sess, args, feed_dict):
        _ = sess.run(self.d_train_op, feed_dict=feed_dict)
        _, results = sess.run([self.g_train_op, self.all_losses], feed_dict=feed_dict)
        return results

    def g_baseline(self, x, args, reuse=False):
        with tf.variable_scope('encoder', reuse=reuse), \
             arg_scope([hem.conv2d],
                       reuse=reuse,
                       filter_size=5,
                       stride=2,
                       padding='VALID',
                       use_batch_norm=args.e_bn,
                       init=tf.contrib.layers.xavier_initializer,
                       activation=tf.nn.relu):        # 65x65x3
            if args.noise_layer == 'x':
                noise = tf.random_uniform([args.batch_size, 1, 65, 65], minval=0, maxval=1)
                e1 = hem.conv2d(tf.concat([x, noise], axis=1),    4,  64, name='e1')  # 31x31x64

            else:
                e1 = hem.conv2d(x,    3,  64, name='e1')

            if args.noise_layer == 'e1':
                noise = tf.random_uniform([args.batch_size, 1, 31, 31], minval=0, maxval=1)
                e2 = hem.conv2d(tf.concat([e1, noise], axis=1),  65, 128, name='e2')  # 14x14x128
            else:
                e2 = hem.conv2d(e1,  64, 128, name='e2')  # 14x14x128

            if args.noise_layer == 'e2':
                noise = tf.random_uniform([args.batch_size, 1, 14, 14], minval=0, maxval=1)
                e3 = hem.conv2d(tf.concat([e2, noise], axis=1), 129, 256, name='e3')  # 5x5x256
            else:
                e3 = hem.conv2d(e2, 128, 256, name='e3')  # 5x5x256

            if args.noise_layer == 'e3':
                noise = tf.random_uniform([args.batch_size, 1, 5, 5], minval=0, maxval=1)
                e4 = hem.conv2d(tf.concat([e3, noise], axis=1), 257, 512, name='e4')  # 1x1x512
            else:
                e4 = hem.conv2d(e3, 256, 512, name='e4')  # 1x1x512

        with tf.variable_scope('decoder', reuse=reuse), \
             arg_scope([hem.deconv2d, hem.conv2d],
                       reuse=reuse,
                       filter_size=5,
                       stride=2,
                       init=tf.contrib.layers.xavier_initializer,
                       padding='VALID',
                       activation=lambda x: hem.lrelu(x, leak=0.2)):                                                # 1x1x512
            # TODO: noise could be of size 512, instead of 1
            if args.noise_layer == 'e4':
                noise = tf.random_uniform([args.batch_size, 1, 1, 1], minval=0, maxval=1)
                y_hat = hem.deconv2d(tf.concat([e4, noise], axis=1), 513, 256, output_shape=(args.batch_size, 256, 5, 5), name='d1')  # 5x5x256
            elif args.noise_layer == 'e4-512':
                noise = tf.random_uniform([args.batch_size, 512, 1, 1], minval=0, maxval=1)
                y_hat = hem.deconv2d(tf.concat([e4, noise], axis=1), 1024, 256,
                                     output_shape=(args.batch_size, 256, 5, 5), name='d1')  # 5x5x256
            else:
                y_hat = hem.deconv2d(e4, 512, 256, output_shape=(args.batch_size, 256, 5, 5), name='d1')                # 5x5x256
            y_hat = tf.concat([y_hat, e3], axis=1)                                                                  # 5x5x512

            if args.noise_layer == 'd2':
                noise = tf.random_uniform([args.batch_size, 1, 5, 5], minval=0, maxval=1)
                y_hat = hem.deconv2d(tf.concat([y_hat, noise], axis=1), 513, 128, output_shape=(args.batch_size, 128, 14, 14), name='d2')  # 14x14x128z
            else:
                y_hat = hem.deconv2d(y_hat, 512, 128, output_shape=(args.batch_size, 128, 14, 14), name='d2')           # 14x14x128z
            y_hat = tf.concat([y_hat, e2], axis=1)                                                                  # 14x14x256

            if args.noise_layer == 'd3':
                noise = tf.random_uniform([args.batch_size, 1, 14, 14], minval=0, maxval=1)
                y_hat = hem.deconv2d(tf.concat([y_hat, noise], axis=1), 257, 64, output_shape=(args.batch_size, 64, 31, 31), name='d3')  # 31x31x64
            else:
                y_hat = hem.deconv2d(y_hat, 256, 64, output_shape=(args.batch_size, 64, 31, 31), name='d3')             # 31x31x64
            y_hat = tf.concat([y_hat, e1], axis=1)                                                                  # 31x31x128

            if args.noise_layer == 'd4':
                noise = tf.random_uniform([args.batch_size, 1, 31, 31], minval=0, maxval=1)
                y_hat = hem.conv2d(tf.concat([y_hat, noise], axis=1), 129, 1, stride=1, filter_size=1, padding='SAME', activation=None, name='d4')  # 31x31x1
            else:
                y_hat = hem.conv2d(y_hat, 128, 1, stride=1, filter_size=1, padding='SAME', activation=None, name='d4')  # 31x31x1
            y_hat = hem.crop_to_bounding_box(y_hat, 0, 0, 29, 29)                                                   # 29x29x1
            #y_hat = tf.maximum(y_hat, tf.zeros_like(y_hat))
        return y_hat

    def d_baseline(self, x, y, args, reuse=False):
        with arg_scope([hem.conv2d],
                       reuse=reuse,
                       activation=lambda x: hem.lrelu(x, leak=0.2),
                       init=tf.contrib.layers.xavier_initializer,
                       padding='VALID',
                       filter_size=5,
                       stride=2):  # x = 65x65x3, y = 29x29x1
            with tf.variable_scope('rgb_path'):
                h1 = hem.conv2d(x, 3, 64, name='hx1')  # 31x31x64
                h1 = hem.conv2d(h1, 64, 128, name='hx2')  # 14x14x128
                h1 = hem.conv2d(h1, 128, 256, name='hx3')  # 5x5x256
                h1 = hem.conv2d(h1, 256, 512, name='hx4')  # 1x1x512
            with tf.variable_scope('depth_path'):
                h2 = hem.conv2d(y, 1, 128, name='hy1')  # 14x14x128
                h2 = hem.conv2d(h2, 128, 256, name='hy2')  # 5x5x256
                h2 = hem.conv2d(h2, 256, 512, name='hy3')  # 1x1x512
            with tf.variable_scope('combined_path'):
                h = tf.concat([h1, h2], axis=1)  # 1x1x1024
                h = hem.conv2d(h, 1024, 1024, stride=1, filter_size=1, padding='SAME', name='h1')  # 1x1x768
                h = hem.conv2d(h, 1024,  512, stride=1, filter_size=1, padding='SAME', name='h2')  # 1x1x384
                h = hem.conv2d(h,  512,    1, stride=1, filter_size=1, padding='SAME', name='h3', activation=None)  # 1x1x1
        # output, logits
        return tf.nn.sigmoid(h), h

    def loss(self, d_real, d_real_logits, d_fake, d_fake_logits, reuse=False):
        def xentropy(logits, labels):
            return tf.nn.sigmoid_cross_entropy_with_logits(logits=logits, labels=labels)

        with tf.variable_scope('loss'):
            with tf.variable_scope('generator'):
                g_fake = tf.reduce_mean(xentropy(d_fake_logits, tf.ones_like(d_fake)), name='g_fake')
            with tf.variable_scope('discriminator'):
                d_real = tf.reduce_mean(xentropy(d_real_logits, tf.ones_like(d_real)), name='d_real')
                d_fake = tf.reduce_mean(xentropy(d_fake_logits, tf.zeros_like(d_fake)), name='d_fake')
                d_total = tf.identity(d_real + d_fake, name='d_total')
            if not reuse:
                hem.add_to_collection('losses', [g_fake, d_real, d_fake, d_total])
        return g_fake, d_total

    # summaries
    ############################################
    def montage_summaries(self, x, y, g, y_hat, args, name=None):
        n_examples = 64
        n = math.floor(math.sqrt(n_examples))
        with tf.variable_scope('montage_preprocess'):
            y_bar = tf.reduce_mean(y, axis=[2, 3], keep_dims=True)
            y_hat = tf.reshape(y_hat, tf.shape(y))
            g = tf.reshape(g, tf.shape(y))
            y = y / 10.0
            y_hat = y_hat / 10.0
            y_bar = y_bar / 10.0
            g = g / 10.0

        with arg_scope([hem.montage],
                       num_examples=n_examples,
                       height=n,
                       width=n,
                       colorize=True):
            ns = 'model' if name is None else 'model_' + name
            with tf.variable_scope(ns):
                hem.montage(x,     name='x')
                hem.montage(y,     name='y')
                hem.montage(tf.ones_like(y) * y_bar, name='y_bar')
                hem.montage(g,     name='g', colorize=False)
                hem.montage(y_hat, name='y_hat')

    def metric_summaries(self, x, y, g, y_hat, args, name=None):
        # from Eigen et. al 2014
        ns = 'metrics' if name is None else 'metrics_' + name
        with tf.variable_scope(ns):
            g = g / 10.0
            y = y / 10.0
            y_hat = y_hat / 10.0

            # standard pixel-wise difference metrics
            abs_rel_diff = tf.reduce_mean(tf.abs(y - y_hat)/y_hat, name='abs_rel_diff')
            squared_rel_diff = tf.reduce_mean(tf.square(y - y_hat)/y_hat)
            linear_rmse = hem.rmse(y, y_hat, name='linear_rmse')
            log_rmse = hem.rmse(tf.log(y + 1e-8), tf.log(y_hat + 1e-8), name='log_rmse')
            tf.summary.scalar('abs_rel_diff', abs_rel_diff)
            tf.summary.scalar('squared_rel_diff', squared_rel_diff)
            tf.summary.scalar('linear_rmse', linear_rmse)
            tf.summary.scalar('log_rmse', log_rmse)

            # scale-invariant rmse
            d = tf.log(y + 1e-8) - tf.log(y_hat + 1e-8)
            n = tf.cast(tf.size(d), tf.float32) # tf.size() = 430592
            scale_invariant_log_rmse = tf.reduce_mean(tf.square(d)) - (tf.reduce_sum(d) ** 2)/(n**2)
            tf.summary.scalar('scale_invariant_log_rmse', scale_invariant_log_rmse)

            # threshold metrics
            delta = tf.maximum(y/y_hat, y_hat/y)
            t1, t1_op = tf.metrics.percentage_below(delta, 1.25,    name='threshold1')
            t2, t2_op = tf.metrics.percentage_below(delta, 1.25**2, name='threshold2')
            t3, t3_op = tf.metrics.percentage_below(delta, 1.25**3, name='threshold3')
            tf.summary.scalar('threshold1', t1_op)
            tf.summary.scalar('threshold2', t2_op)
            tf.summary.scalar('threshold3', t3_op)

            # per-image stats
            per_image_rmse = tf.reduce_mean(tf.sqrt(tf.square(y - y_hat)), axis=[1, 2, 3])
            tf.summary.scalar('per_image_rmse/mean', tf.reduce_mean(per_image_rmse))
            tf.summary.scalar('per_image_rmse/min', tf.reduce_min(per_image_rmse))
            hem.summarize_moments(g, 'g_moments', args)
            hem.summarize_moments(y_hat, 'y_hat_moments', args)
