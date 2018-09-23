import tensorflow as tf

import graph_manager
import net
import eval_utils
import loss_utils
import tf_inputs
import tf_utils


"""Helper functions to build the train and eval graph for ODGI."""


def forward_pass(inputs, 
                 outputs, 
                 configuration,
                 is_training=True,
                 reuse=False, 
                 reuse_activations=False,
                 scope_name='model',
                 use_same_activations_scope=False,
                 verbose=False):
    """Forward-pass in the net"""
    network = graph_manager.get_defaults(configuration, ['network'], verbose=True)[0]
    activations_scope_name = scope_name if not use_same_activations_scope else 'stage'
    with tf.variable_scope(activations_scope_name, reuse=reuse or reuse_activations):        
        if network == 'tiny-yolov2':
            activations = net.tiny_yolo_v2(
                inputs["image"], is_training=is_training, reuse=reuse or reuse_activations, verbose=verbose, **configuration)
        elif network == 'yolov2':
            activations = net.yolo_v2(
                inputs["image"], is_training=is_training, reuse=reuse or reuse_activations, verbose=verbose, **configuration)
        else:
            raise NotImplementedError('Uknown network architecture', network)
    with tf.variable_scope(scope_name, reuse=reuse):        
        net.get_detection_with_groups_outputs(
            activations, outputs, reuse=reuse, verbose=verbose, **configuration)
            
            
def train_pass(inputs, configuration, intermediate_stage=False, is_chief=False, 
               use_same_activations_scope=False, reuse_activations=False, verbose=1):
    """ Compute outputs of the net and add losses to the graph.
    
    Args:
        inputs: Dictionnary of inputs
        configuration: Configuration dictionnary
        intermediate_stage: If True, filter the obtain boxes given the configuration group to get the 
            image regions coordinates to extract at the next stage
        is_chief: Whether the current training device is chief (verbosity and summaries)
        verbose: verbosity level
        
    Returns:
        Dictionnary of outputs
    """
    outputs = {}
    dev_verbose = verbose * is_chief
    base_name = graph_manager.get_defaults(configuration, ['base_name'], verbose=dev_verbose)[0]
    if dev_verbose == 2:
        print(' \033[31m> %s\033[0m' % base_name)
    elif dev_verbose == 1:
        print(' > %s' % base_name)
        
    # Feed forward
    with tf.name_scope('%s/net' % base_name):
        forward_pass(inputs, outputs, configuration, scope_name=base_name, is_training=True, reuse=not is_chief,
                     reuse_activations=reuse_activations, use_same_activations_scope=use_same_activations_scope, verbose=dev_verbose) 
        
    # Compute crops to feed to the next stage
    if intermediate_stage:
        with tf.name_scope('extract_patches'):
            tf_inputs.extract_groups(inputs, outputs, mode='train', verbose=dev_verbose, **configuration)  
        
    # Add losses
    with tf.name_scope('%s/loss' % base_name):
        if intermediate_stage:
            loss_fn = loss_utils.get_odgi_loss
        else:
            loss_fn = loss_utils.get_standard_loss
        graph_manager.add_losses_to_graph(
            loss_fn, inputs, outputs, configuration, is_chief=is_chief, verbose=is_chief)
        
    # Display found losses
    if dev_verbose == 1:
        print('\n'.join("    *%s*: shape=%s, dtype=%s" % (
            key, value.get_shape().as_list(), value.dtype) for key, value in outputs.items()))
    elif dev_verbose == 2:
        print('\n'.join("    \x1b[32m*%s*\x1b[0m: shape=%s, dtype=%s" % (
            key, value.get_shape().as_list(), value.dtype) for key, value in outputs.items()))
    return outputs


def feed_pass(inputs, outputs, configuration, mode='train', is_chief=True, verbose=False):
    """
        Args:
            inputs: inputs dictionnary
            outputs: outputs dictionnary
            configuration: config dictionnary
        
        Returns:
            Dictionnary of inputs for the next stage
    """
    dev_verbose = verbose * is_chief
    if dev_verbose: print(' > create stage 2 inputs:')
    return graph_manager.get_stage2_inputs(
        inputs, outputs['crop_boxes'], mode=mode, verbose=dev_verbose, **configuration)
        
    
def eval_pass_intermediate_stage(inputs, configuration, use_same_activations_scope=False, verbose=False):
    """ Evaluation pass for intermediate stages."""
    outputs = {}
    base_name = graph_manager.get_defaults(configuration, ['base_name'], verbose=verbose)[0]
    if verbose == 2:
        print(' \033[31m> %s\033[0m' % base_name)
    elif verbose == 1:
        print(' > %s' % base_name)
        
    # Feed forward
    with tf.name_scope('%s/net' % base_name):
        forward_pass(inputs, outputs, configuration, scope_name=base_name, is_training=False, 
                     use_same_activations_scope=use_same_activations_scope, reuse=True, verbose=verbose) 
        
    # Compute crops to feed to the next stage
    with tf.name_scope('extract_patches'):
        tf_inputs.extract_groups(inputs, outputs, mode='test', verbose=verbose, **configuration)
        
    return outputs    


def eval_pass_final_stage(stage2_inputs, stage1_inputs, stage1_outputs, configuration, use_same_activations_scope=False, verbose=False):
    """ Evaluation for the full pipeline.
        Args:
            stage2_inputs: inputs dictionnary for stage2
            stage1_inputs: inputs dictionnary for stage 1
            stage1_outputs: outputs dictionnary for stage1
            configuration: config dictionnary
            metrics_to_norms: Map metrics key to normalizer key
            clear_metrics_op: List to be updated with reset operations
            update_metrics_op: List to be updated with update operation
            device: Current device number to be used in the variable scope for each metric
        
        Returns:
            Dictionnary of outputs, merge by image
            Dictionnary of unscaled ouputs (for summary purposes)
    """
    outputs = {}
    base_name = graph_manager.get_defaults(configuration, ['base_name'], verbose=verbose)[0]
    if verbose == 2:
        print(' \033[31m> %s\033[0m' % base_name)
    elif verbose == 1:
        print(' > %s' % base_name)
    
    # Feed forward
    with tf.name_scope('net'):
        forward_pass(stage2_inputs, outputs, configuration, scope_name=base_name, is_training=False,
                     use_same_activations_scope=use_same_activations_scope, reuse=True, verbose=verbose) 
            
    # Reshape outputs from stage2 to stage1
    crop_boxes = stage1_outputs["crop_boxes"]  
    num_crops = crop_boxes.get_shape()[1].value
    num_boxes = outputs['bounding_boxes'].get_shape()[-2].value
    num_cells = outputs['bounding_boxes'].get_shape()[-2].value
    # for summary
    with tf.name_scope('reshape_outputs'):
        #batch_size = graph_manager.get_defaults(configuration, ['batch_size'], verbose=verbose)[0]
        # outputs:  (stage1_batch * num_crops, num_cell, num_cell, num_boxes, ...)
        # to: (stage1_batch, num_cell, num_cell, num_boxes * num_crops, ...)
        for key, value in outputs.items():            
            shape = tf.shape(value)
            batch_shape = tf.stack([-1, num_crops])            
            new_shape = tf.concat([batch_shape, shape[1:]], axis=0)
            batches = tf.reshape(value, new_shape)
            batches = tf.concat(tf.unstack(batches, num=num_crops, axis=1), axis=3)
            outputs[key] = tf.stack(batches, axis=0)
    
    # Rescale bounding boxes from stage2 to stage1
    with tf.name_scope('rescale_bounding_boxes'):
        # crop_boxes: (stage1_batch, 1, 1, num_crops * num_boxes, 4)
        crop_boxes = tf.expand_dims(crop_boxes, axis=-2)
        crop_boxes = tf.tile(crop_boxes, (1, 1, num_boxes, 1))
        crop_boxes = tf.reshape(crop_boxes, (-1, 1, 1, num_crops * num_boxes, 4))
        crop_mins, crop_maxs = tf.split(crop_boxes, 2, axis=-1)
        # bounding_boxes: (stage1_batch, num_cells, num_cells, num_crops * num_boxes, 4)
        bounding_boxes = outputs['bounding_boxes']
        bounding_boxes *= tf.maximum(1e-8, tf.tile(crop_maxs - crop_mins, (1, 1, 1, 1, 2)))
        bounding_boxes += tf.tile(crop_mins, (1, 1, 1, 1, 2))
        bounding_boxes = tf.clip_by_value(bounding_boxes, 0., 1.)
        outputs['bounding_boxes'] = bounding_boxes
        
    return outputs