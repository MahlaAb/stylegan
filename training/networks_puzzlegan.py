# Copyright (c) 2019, NVIDIA CORPORATION. All rights reserved.
#
# This work is licensed under the Creative Commons Attribution-NonCommercial
# 4.0 International License. To view a copy of this license, visit
# http://creativecommons.org/licenses/by-nc/4.0/ or send a letter to
# Creative Commons, PO Box 1866, Mountain View, CA 94042, USA.

"""Network architectures used in the ProGAN paper."""

import numpy as np
import tensorflow as tf

# NOTE: Do not import any application-specific modules here!
# Specify all network parameters as kwargs.

#----------------------------------------------------------------------------

def lerp(a, b, t): return a + (b - a) * t
def lerp_clip(a, b, t): return a + (b - a) * tf.clip_by_value(t, 0.0, 1.0)
def cset(cur_lambda, new_cond, new_lambda): return lambda: tf.cond(new_cond, new_lambda, cur_lambda)

#----------------------------------------------------------------------------
# Get/create weight tensor for a convolutional or fully-connected layer.

def get_weight(shape, gain=np.sqrt(2), use_wscale=False):
    fan_in = np.prod(shape[:-1]) # [kernel, kernel, fmaps_in, fmaps_out] or [in, out]
    std = gain / np.sqrt(fan_in) # He init
    if use_wscale:
        wscale = tf.constant(np.float32(std), name='wscale')
        w = tf.get_variable('weight', shape=shape, initializer=tf.initializers.random_normal()) * wscale
    else:
        w = tf.get_variable('weight', shape=shape, initializer=tf.initializers.random_normal(0, std))
    return w

#----------------------------------------------------------------------------
# Fully-connected layer.

def dense(x, fmaps, gain=np.sqrt(2), use_wscale=False):
    if len(x.shape) > 2:
        x = tf.reshape(x, [-1, np.prod([d.value for d in x.shape[1:]])])
    w = get_weight([x.shape[1].value, fmaps], gain=gain, use_wscale=use_wscale)
    w = tf.cast(w, x.dtype)
    return tf.matmul(x, w)

#----------------------------------------------------------------------------
# Convolutional layer.

def conv2d(x, fmaps, kernel, gain=np.sqrt(2), use_wscale=False):
    assert kernel >= 1 and kernel % 2 == 1
    w = get_weight([kernel, kernel, x.shape[1].value, fmaps], gain=gain, use_wscale=use_wscale)
    w = tf.cast(w, x.dtype)
    return tf.nn.conv2d(x, w, strides=[1,1,1,1], padding='SAME', data_format='NCHW')

#----------------------------------------------------------------------------
# Apply bias to the given activation tensor.

def apply_bias(x):
    b = tf.get_variable('bias', shape=[x.shape[1]], initializer=tf.initializers.zeros())
    b = tf.cast(b, x.dtype)
    if len(x.shape) == 2:
        return x + b
    return x + tf.reshape(b, [1, -1, 1, 1])

#----------------------------------------------------------------------------
# Leaky ReLU activation. Same as tf.nn.leaky_relu, but supports FP16.

def leaky_relu(x, alpha=0.2):
    with tf.name_scope('LeakyRelu'):
        alpha = tf.constant(alpha, dtype=x.dtype, name='alpha')
        return tf.maximum(x * alpha, x)

#----------------------------------------------------------------------------
# Nearest-neighbor upscaling layer.

def upscale2d(x, factor=2):
    assert isinstance(factor, int) and factor >= 1
    if factor == 1: return x
    with tf.variable_scope('Upscale2D'):
        s = x.shape
        x = tf.reshape(x, [-1, s[1], s[2], 1, s[3], 1])
        x = tf.tile(x, [1, 1, 1, factor, 1, factor])
        x = tf.reshape(x, [-1, s[1], s[2] * factor, s[3] * factor])
        return x

#----------------------------------------------------------------------------
# Fused upscale2d + conv2d.
# Faster and uses less memory than performing the operations separately.

def upscale2d_conv2d(x, fmaps, kernel, gain=np.sqrt(2), use_wscale=False):
    assert kernel >= 1 and kernel % 2 == 1
    w = get_weight([kernel, kernel, x.shape[1].value, fmaps], gain=gain, use_wscale=use_wscale)
    w = tf.transpose(w, [0, 1, 3, 2]) # [kernel, kernel, fmaps_out, fmaps_in]
    w = tf.pad(w, [[1,1], [1,1], [0,0], [0,0]], mode='CONSTANT')
    w = tf.add_n([w[1:, 1:], w[:-1, 1:], w[1:, :-1], w[:-1, :-1]])
    w = tf.cast(w, x.dtype)
    os = [tf.shape(x)[0], fmaps, x.shape[2] * 2, x.shape[3] * 2]
    return tf.nn.conv2d_transpose(x, w, os, strides=[1,1,2,2], padding='SAME', data_format='NCHW')

#----------------------------------------------------------------------------
# Box filter downscaling layer.

def downscale2d(x, factor=2):
    assert isinstance(factor, int) and factor >= 1
    if factor == 1: return x
    with tf.variable_scope('Downscale2D'):
        ksize = [1, 1, factor, factor]
        return tf.nn.avg_pool(x, ksize=ksize, strides=ksize, padding='VALID', data_format='NCHW') # NOTE: requires tf_config['graph_options.place_pruned_graph'] = True

#----------------------------------------------------------------------------
# Fused conv2d + downscale2d.
# Faster and uses less memory than performing the operations separately.

def conv2d_downscale2d(x, fmaps, kernel, gain=np.sqrt(2), use_wscale=False):
    assert kernel >= 1 and kernel % 2 == 1
    w = get_weight([kernel, kernel, x.shape[1].value, fmaps], gain=gain, use_wscale=use_wscale)
    w = tf.pad(w, [[1,1], [1,1], [0,0], [0,0]], mode='CONSTANT')
    w = tf.add_n([w[1:, 1:], w[:-1, 1:], w[1:, :-1], w[:-1, :-1]]) * 0.25
    w = tf.cast(w, x.dtype)
    return tf.nn.conv2d(x, w, strides=[1,1,2,2], padding='SAME', data_format='NCHW')

#----------------------------------------------------------------------------
# Pixelwise feature vector normalization.

def pixel_norm(x, epsilon=1e-8):
    with tf.variable_scope('PixelNorm'):
        return x * tf.rsqrt(tf.reduce_mean(tf.square(x), axis=1, keepdims=True) + epsilon)

#----------------------------------------------------------------------------
# Minibatch standard deviation.

def minibatch_stddev_layer(x, group_size=4, num_new_features=1):
    with tf.variable_scope('MinibatchStddev'):
        group_size = tf.minimum(group_size, tf.shape(x)[0])     # Minibatch must be divisible by (or smaller than) group_size.
        s = x.shape                                             # [NCHW]  Input shape.
        y = tf.reshape(x, [group_size, -1, num_new_features, s[1]//num_new_features, s[2], s[3]])   # [GMncHW] Split minibatch into M groups of size G. Split channels into n channel groups c.
        y = tf.cast(y, tf.float32)                              # [GMncHW] Cast to FP32.
        y -= tf.reduce_mean(y, axis=0, keepdims=True)           # [GMncHW] Subtract mean over group.
        y = tf.reduce_mean(tf.square(y), axis=0)                # [MncHW]  Calc variance over group.
        y = tf.sqrt(y + 1e-8)                                   # [MncHW]  Calc stddev over group.
        y = tf.reduce_mean(y, axis=[2,3,4], keepdims=True)      # [Mn111]  Take average over fmaps and pixels.
        y = tf.reduce_mean(y, axis=[2])                         # [Mn11] Split channels into c channel groups
        y = tf.cast(y, x.dtype)                                 # [Mn11]  Cast back to original data type.
        y = tf.tile(y, [group_size, 1, s[2], s[3]])             # [NnHW]  Replicate over group and pixels.
        return tf.concat([x, y], axis=1)                        # [NCHW]  Append as new fmap.


def apply_noise(x, noise_var=None, randomize_noise=True):
    assert len(x.shape) == 4 # NCHW
    with tf.variable_scope('Noise'):
        if noise_var is None or randomize_noise:
            noise = tf.random_normal([tf.shape(x)[0], 1, x.shape[2], x.shape[3]], dtype=x.dtype)
        else:
            noise = tf.cast(noise_var, x.dtype)
        weight = tf.get_variable('weight', shape=[x.shape[1].value], initializer=tf.initializers.zeros())
        return x + noise * tf.reshape(tf.cast(weight, x.dtype), [1, -1, 1, 1])
    
#----------------------------------------------------------------------------
# Networks used in the ProgressiveGAN paper.

def G_puzzle(
    latents_in,                         # First input: Latent vectors [minibatch, latent_size].
    labels_in,                          # Second input: Labels [minibatch, label_size].
    num_channels        = 1,            # Number of output color channels. Overridden based on dataset.
    mode                = None,
    latents_sizes       = [512],
    firstblock_res      = 8,
    resolution          = 32,           # Output resolution. Overridden based on dataset.
    label_size          = 0,            # Dimensionality of the labels, 0 if no labels. Overridden based on dataset.
    fmap_base           = 8192,         # Overall multiplier for the number of feature maps.
    fmap_decay          = 1.0,          # log2 feature map reduction when doubling the resolution.
    fmap_max            = 512,          # Maximum number of feature maps in any layer.
    latent_size         = None,         # Dimensionality of the latent vectors. None = min(fmap_base, fmap_max).
    normalize_latents   = True,         # Normalize latent vectors before feeding them to the network?
    use_wscale          = True,         # Enable equalized learning rate?
    use_pixelnorm       = True,         # Enable pixelwise feature vector normalization?
    pixelnorm_epsilon   = 1e-8,         # Constant epsilon for pixelwise feature vector normalization.
    use_leakyrelu       = True,         # True = leaky ReLU, False = ReLU.
    dtype               = 'float32',    # Data type to use for activations and outputs.
    fused_scale         = True,         # True = use fused upscale2d + conv2d, False = separate upscale2d layers.
    structure           = None,         # 'linear' = human-readable, 'recursive' = efficient, None = select automatically.
    is_template_graph   = False,        # True = template graph constructed by the Network class, False = actual evaluation.
    **_kwargs):                         # Ignore unrecognized keyword args.

    resolution_log2 = int(np.log2(resolution))
    assert resolution == 2**resolution_log2 and resolution >= 4
    def nf(stage): return min(int(fmap_base / (2.0 ** (stage * fmap_decay))), fmap_max)
    def PN(x): return pixel_norm(x, epsilon=pixelnorm_epsilon) if use_pixelnorm else x
    if latent_size is None: latent_size = nf(0)
    if structure is None: structure = 'linear' if is_template_graph else 'recursive'
    act = leaky_relu if use_leakyrelu else tf.nn.relu
    
    firstblock_res_log2 = int(np.log2(firstblock_res))

    latents_in.set_shape([None, latent_size])
    labels_in.set_shape([None, label_size])
    combo_in = tf.cast(latents_in, dtype)
    lod_in = tf.cast(tf.get_variable('lod', initializer=np.float32(0.0), trainable=False), dtype)
    images_out = None
    
    # Building blocks.
    def block(x, res): # res = 2..resolution_log2
        with tf.variable_scope('%dx%d' % (2**res, 2**res)):
            if res == firstblock_res_log2: # firstblock_res x firstblock_res
                if normalize_latents: x = pixel_norm(x, epsilon=pixelnorm_epsilon)
                x = first_layer(x, mode, res)
                with tf.variable_scope('Conv'):
                    x = conv2d(x, fmaps=nf(res-1), kernel=3, use_wscale=use_wscale)
                    x = PN(act(apply_bias(x)))
            else: # other blocks
                if fused_scale:
                    with tf.variable_scope('Conv0_up'):
                        x = upscale2d_conv2d(x, fmaps=nf(res-1), kernel=3, use_wscale=use_wscale)
                        x = PN(act(apply_bias(x)))
                else:
                    x = upscale2d(x)
                    with tf.variable_scope('Conv0'):
                        x = conv2d(x, fmaps=nf(res-1), kernel=3, use_wscale=use_wscale)
                        x = PN(act(apply_bias(x)))
                with tf.variable_scope('Conv1'):
                    x = conv2d(x, fmaps=nf(res-1), kernel=3, use_wscale=use_wscale)
                    x = PN(act(apply_bias(x)))
            return x
    
    def first_layer(x, mode, res):
        
        s = 0
        for idx, size in enumerate(latents_sizes):
            if idx == 0:
                z = [(tf.slice(x, [0, 0], [-1, latents_sizes[idx]]))]
            else:
                z.append(tf.slice(x, [0, s], [-1, latents_sizes[idx]]))
            s += latents_sizes[idx]
        
        if mode == '2parts-faces':
            
            with tf.variable_scope('Dense11'):
                x11 = dense(z[0], fmaps=nf(res-1)*16, gain=np.sqrt(2)/4, use_wscale=use_wscale)
                x11 = tf.reshape(x11, [-1, nf(res-1), 2, 8])
            with tf.variable_scope('Dense12'):
                x12 = dense(z[0], fmaps=nf(res-1)*8, gain=np.sqrt(2)/4, use_wscale=use_wscale)
                x12 = tf.reshape(x12, [-1, nf(res-1), 4, 2])
            with tf.variable_scope('Dense13'):
                x13 = dense(z[0], fmaps=nf(res-1)*16, gain=np.sqrt(2)/4, use_wscale=use_wscale)
                x13 = tf.reshape(x13, [-1, nf(res-1), 2, 8])
            with tf.variable_scope('Dense14'):
                x14 = dense(z[0], fmaps=nf(res-1)*8, gain=np.sqrt(2)/4, use_wscale=use_wscale)
                x14 = tf.reshape(x14, [-1, nf(res-1), 4, 2])
                
            with tf.variable_scope('Dense2'):
                x2 = dense(z[1], fmaps=nf(res-1)*16, gain=np.sqrt(2)/4, use_wscale=use_wscale)
                x2 = tf.reshape(x2, [-1, nf(res-1), 4, 4])
                
            x = tf.concat((x11, tf.concat((x12, x2, x14), axis=3), x13), axis=2) # x = (?, 512, 8, 8)
            
        if mode == '5parts-faces':
            
            with tf.variable_scope('Dense11'):
                x11 = dense(z[0], fmaps=nf(res-1)*16, gain=np.sqrt(2)/4, use_wscale=use_wscale)
                x11 = tf.reshape(x11, [-1, nf(res-1), 8, 2])
            with tf.variable_scope('Dense12'):
                x12 = dense(z[0], fmaps=nf(res-1)*4, gain=np.sqrt(2)/4, use_wscale=use_wscale)
                x12 = tf.reshape(x12, [-1, nf(res-1), 1, 4])
            with tf.variable_scope('Dense13'):
                x13 = dense(z[0], fmaps=nf(res-1)*16, gain=np.sqrt(2)/4, use_wscale=use_wscale)
                x13 = tf.reshape(x13, [-1, nf(res-1), 8, 2])
                  
            with tf.variable_scope('Dense2'):
                x2 = dense(z[1], fmaps=nf(res-1)*8, gain=np.sqrt(2)/4, use_wscale=use_wscale) 
                x2 = tf.reshape(x2, [-1, nf(res-1), 2, 4])
                
            with tf.variable_scope('Dense3'):
                x3 = dense(z[2], fmaps=nf(res-1)*8, gain=np.sqrt(2)/4, use_wscale=use_wscale) 
                x3 = tf.reshape(x3, [-1, nf(res-1), 2, 4])
                
            with tf.variable_scope('Dense4'):
                x4 = dense(z[3], fmaps=nf(res-1)*4, gain=np.sqrt(2)/4, use_wscale=use_wscale) 
                x4 = tf.reshape(x4, [-1, nf(res-1), 2, 2])
                
            with tf.variable_scope('Dense51'):
                x51 = dense(z[4], fmaps=nf(res-1)*3, gain=np.sqrt(2)/4, use_wscale=use_wscale)
                x51 = tf.reshape(x51, [-1, nf(res-1), 3, 1])
            with tf.variable_scope('Dense52'):
                x52 = dense(z[4], fmaps=nf(res-1)*2, gain=np.sqrt(2)/4, use_wscale=use_wscale)
                x52 = tf.reshape(x52, [-1, nf(res-1), 1, 2])
            with tf.variable_scope('Dense53'):
                x53 = dense(z[4], fmaps=nf(res-1)*3, gain=np.sqrt(2)/4, use_wscale=use_wscale)
                x53 = tf.reshape(x53, [-1, nf(res-1), 3, 1])
                
            x = tf.concat((x51, tf.concat((x4, x52), axis=2), x53), axis=3) # x = (?, 512, 3, 4)
            x = tf.concat((x12, x2, x3, x), axis=2) # x = (?, 512, 8, 4)
            x = tf.concat((x11, x, x13), axis=3) # x = (?, 512, 8, 8)
            
        if mode == '2parts-bedrooms':
            
            with tf.variable_scope('Dense1'):
                x1 = dense(z[0], fmaps=nf(res-1)*32, gain=np.sqrt(2)/4, use_wscale=use_wscale)
                x1 = tf.reshape(x1, [-1, nf(res-1), 4, 8])
                
            with tf.variable_scope('Dense2'):
                x2 = dense(z[1], fmaps=nf(res-1)*32, gain=np.sqrt(2)/4, use_wscale=use_wscale) 
                x2 = tf.reshape(x2, [-1, nf(res-1), 4, 8])
                
            x = tf.concat((x1, x2), axis=2) # x = (?, 512, 8, 8)
                
        if mode == '4parts-digits':
            
            with tf.variable_scope('Dense1'):
                x1 = dense(z[0], fmaps=nf(res-1)*64, gain=np.sqrt(2)/4, use_wscale=use_wscale)
                x1 = tf.reshape(x1, [-1, nf(res-1), 8, 8])
                
            with tf.variable_scope('Dense2'):
                x2 = dense(z[1], fmaps=nf(res-1)*64, gain=np.sqrt(2)/4, use_wscale=use_wscale) 
                x2 = tf.reshape(x2, [-1, nf(res-1), 8, 8])
                
            with tf.variable_scope('Dense3'):
                x3 = dense(z[2], fmaps=nf(res-1)*64, gain=np.sqrt(2)/4, use_wscale=use_wscale)
                x3 = tf.reshape(x3, [-1, nf(res-1), 8, 8])
                
            with tf.variable_scope('Dense4'):
                x4 = dense(z[3], fmaps=nf(res-1)*64, gain=np.sqrt(2)/4, use_wscale=use_wscale) 
                x4 = tf.reshape(x4, [-1, nf(res-1), 8, 8])
            
            x = tf.concat((tf.concat((x1, x2), axis=3), tf.concat((x3, x4), axis=3)), axis=2) # x = (?, 512, 16, 16)

        return PN(act(apply_bias(x)))
                
    def torgb(x, res): # res = 2..resolution_log2
        lod = resolution_log2 - res
        with tf.variable_scope('ToRGB_lod%d' % lod):
            return apply_bias(conv2d(x, fmaps=num_channels, kernel=1, gain=1, use_wscale=use_wscale))

    # Linear structure: simple but inefficient.
    if structure == 'linear':
        x = block(combo_in, firstblock_res_log2)
        images_out = torgb(x, firstblock_res_log2)
        for res in range(firstblock_res_log2 + 1, resolution_log2 + 1):
            lod = resolution_log2 - res
            x = block(x, res)
            img = torgb(x, res)
            images_out = upscale2d(images_out)
            with tf.variable_scope('Grow_lod%d' % lod):
                images_out = lerp_clip(img, images_out, lod_in - lod)

    # Recursive structure: complex but efficient.
    if structure == 'recursive':
        def grow(x, res, lod):
            y = block(x, res)
            img = lambda: upscale2d(torgb(y, res), 2**lod)
            if res > firstblock_res_log2: img = cset(img, (lod_in > lod), lambda: upscale2d(lerp(torgb(y, res), upscale2d(torgb(x, res - 1)), lod_in - lod), 2**lod))
            if lod > 0: img = cset(img, (lod_in < lod), lambda: grow(y, res + 1, lod - 1))
            return img()
        images_out = grow(combo_in, firstblock_res_log2, resolution_log2 - firstblock_res_log2)

    assert images_out.dtype == tf.as_dtype(dtype)
    images_out = tf.identity(images_out, name='images_out')
    return images_out


def D_puzzle(
    images_in,                          # First input: Images [minibatch, channel, height, width].
    labels_in,                          # Second input: Labels [minibatch, label_size].
    num_channels        = 1,            # Number of input color channels. Overridden based on dataset.
    firstblock_res      = 8,
    resolution          = 32,           # Input resolution. Overridden based on dataset.
    label_size          = 0,            # Dimensionality of the labels, 0 if no labels. Overridden based on dataset.
    fmap_base           = 8192,         # Overall multiplier for the number of feature maps.
    fmap_decay          = 1.0,          # log2 feature map reduction when doubling the resolution.
    fmap_max            = 512,          # Maximum number of feature maps in any layer.
    use_wscale          = True,         # Enable equalized learning rate?
    mbstd_group_size    = 4,            # Group size for the minibatch standard deviation layer, 0 = disable.
    dtype               = 'float32',    # Data type to use for activations and outputs.
    fused_scale         = True,         # True = use fused conv2d + downscale2d, False = separate downscale2d layers.
    structure           = None,         # 'linear' = human-readable, 'recursive' = efficient, None = select automatically
    is_template_graph   = False,        # True = template graph constructed by the Network class, False = actual evaluation.
    **_kwargs):                         # Ignore unrecognized keyword args.

    resolution_log2 = int(np.log2(resolution))
    assert resolution == 2**resolution_log2 and resolution >= 4
    def nf(stage): return min(int(fmap_base / (2.0 ** (stage * fmap_decay))), fmap_max)
    if structure is None: structure = 'linear' if is_template_graph else 'recursive'
    act = leaky_relu
    
    firstblock_res_log2 = int(np.log2(firstblock_res))

    images_in.set_shape([None, num_channels, resolution, resolution])
    labels_in.set_shape([None, label_size])
    images_in = tf.cast(images_in, dtype)
    labels_in = tf.cast(labels_in, dtype)
    lod_in = tf.cast(tf.get_variable('lod', initializer=np.float32(0.0), trainable=False), dtype)
    scores_out = None

    # Building blocks.
    def fromrgb(x, res): # res = 2..resolution_log2
        with tf.variable_scope('FromRGB_lod%d' % (resolution_log2 - res)):
            return act(apply_bias(conv2d(x, fmaps=nf(res-1), kernel=1, use_wscale=use_wscale)))
    def block(x, res): # res = 2..resolution_log2
        with tf.variable_scope('%dx%d' % (2**res, 2**res)):
            if res > firstblock_res_log2: # 8x8 and up
                with tf.variable_scope('Conv0'):
                    x = act(apply_bias(conv2d(x, fmaps=nf(res-1), kernel=3, use_wscale=use_wscale)))
                if fused_scale:
                    with tf.variable_scope('Conv1_down'):
                        x = act(apply_bias(conv2d_downscale2d(x, fmaps=nf(res-2), kernel=3, use_wscale=use_wscale)))
                else:
                    with tf.variable_scope('Conv1'):
                        x = act(apply_bias(conv2d(x, fmaps=nf(res-2), kernel=3, use_wscale=use_wscale)))
                    x = downscale2d(x)
            else: # firstblock_res x firstblock_res
                if mbstd_group_size > 1:
                    x = minibatch_stddev_layer(x, mbstd_group_size)
                with tf.variable_scope('Conv'):
                    x = act(apply_bias(conv2d(x, fmaps=nf(res-1), kernel=3, use_wscale=use_wscale)))
                with tf.variable_scope('Dense0'):
                    x = act(apply_bias(dense(x, fmaps=nf(res-2), use_wscale=use_wscale)))
                with tf.variable_scope('Dense1'):
                    x = apply_bias(dense(x, fmaps=1, gain=1, use_wscale=use_wscale))
            return x

    # Linear structure: simple but inefficient.
    if structure == 'linear':
        img = images_in
        x = fromrgb(img, resolution_log2)
        for res in range(resolution_log2, firstblock_res_log2, -1):
            lod = resolution_log2 - res
            x = block(x, res)
            img = downscale2d(img)
            y = fromrgb(img, res - 1)
            with tf.variable_scope('Grow_lod%d' % lod):
                x = lerp_clip(x, y, lod_in - lod)
        scores_out = block(x, firstblock_res_log2)

    # Recursive structure: complex but efficient.
    if structure == 'recursive':
        def grow(res, lod):
            x = lambda: fromrgb(downscale2d(images_in, 2**lod), res)
            if lod > 0: x = cset(x, (lod_in < lod), lambda: grow(res + 1, lod - 1))
            x = block(x(), res); y = lambda: x
            if res > firstblock_res_log2: y = cset(y, (lod_in > lod), lambda: lerp(x, fromrgb(downscale2d(images_in, 2**(lod+1)), res - 1), lod_in - lod))
            return y()
        scores_out = grow(firstblock_res_log2, resolution_log2 - firstblock_res_log2)

    assert scores_out.dtype == tf.as_dtype(dtype)
    scores_out = tf.identity(scores_out, name='scores_out')
    return scores_out

#----------------------------------------------------------------------------
