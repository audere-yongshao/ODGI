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
                 verbose=False,
                 scope_name='model'):
    """Forward-pass in the net"""
    with tf.variable_scope(scope_name, reuse=reuse):        
        activations = net.tiny_yolo_v2(
            inputs["image"], is_training=is_training, reuse=reuse, verbose=verbose, **configuration)
        net.get_detection_with_groups_outputs(
            activations, outputs, reuse=reuse, verbose=verbose, **configuration)
            
            
def train_pass(inputs, configuration, intermediate_stage=False, is_chief=False):
    """ Compute outputs of the net and add losses to the graph.
    """
    outputs = {}
    base_name = graph_manager.get_defaults(configuration, ['base_name'], verbose=is_chief)[0]
    if is_chief: print(' > %s:' % base_name)
        
    # Feed forward
    with tf.name_scope('%s/net' % base_name):
        forward_pass(inputs, outputs, configuration, scope_name=base_name, 
                     is_training=True, reuse=not is_chief, verbose=is_chief) 
        
    # Compute crops to feed to the next stage
    if intermediate_stage:
        with tf.name_scope('extract_patches'):
            tf_inputs.extract_groups(inputs, outputs, mode='train', verbose=is_chief, **configuration)  
        
    # Add losses
    with tf.name_scope('%s/loss' % base_name):
        if intermediate_stage:
            loss_fn = loss_utils.get_odgi_loss
        else:
            loss_fn = loss_utils.get_standard_loss
        graph_manager.add_losses_to_graph(
            loss_fn, inputs, outputs, configuration, is_chief=is_chief, verbose=is_chief)
        
    if is_chief:
        print('\n'.join("    *%s*: shape=%s, dtype=%s" % (
            key, value.get_shape().as_list(), value.dtype) for key, value in outputs.items()))
    return outputs


def feed_pass(inputs, outputs, configuration, mode='train', is_chief=False, verbose=False):
    """
        Args:
            inputs: inputs dictionnary
            outputs: outputs dictionnary
            configuration: config dictionnary
        
        Returns:
            Dictionnary of inputs for the next stage
    """
    if is_chief: print(' > create stage 2 inputs:')
    return graph_manager.get_stage2_inputs(
        inputs, outputs['crop_boxes'], mode=mode, verbose=verbose, **configuration)
        
    
def eval_pass_intermediate_stage(inputs, configuration, metrics_to_norms, clear_metrics_op, 
                                 update_metrics_op, device=0, is_chief=False):
    """ Evaluation pass for intermediate stages."""
    outputs = {}
    base_name = graph_manager.get_defaults(configuration, ['base_name'], verbose=is_chief)[0]
    if is_chief: print(' > %s:' % base_name)
        
    # Feed forward
    with tf.name_scope('%s/net' % base_name):
        forward_pass(inputs, outputs, configuration, scope_name=base_name, is_training=False, 
                     reuse=True, verbose=is_chief) 
        
    # Compute crops to feed to the next stage
    with tf.name_scope('extract_patches'):
        tf_inputs.extract_groups(inputs, outputs, mode='test', verbose=is_chief, **configuration)        
        
    with tf.name_scope('%s/eval' % base_name):
        # Add number of samples counter
        graph_manager.add_metrics_to_graph(
            eval_utils.get_samples_running_counters, inputs, outputs, metrics_to_norms, clear_metrics_op, 
            update_metrics_op, configuration, device=device, verbose=is_chief) 
        # Add metrics
        graph_manager.add_metrics_to_graph(
            eval_utils.get_odgi_eval, inputs, outputs, metrics_to_norms, clear_metrics_op, 
            update_metrics_op, configuration, device=device, verbose=is_chief)     
        
    return outputs    


def eval_pass_final_stage(stage2_inputs, stage1_inputs, stage1_outputs, configuration, metrics_to_norms, 
                          clear_metrics_op, update_metrics_op, device=0, is_chief=False):
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
    base_name = graph_manager.get_defaults(configuration, ['base_name'], verbose=is_chief)[0]
    if is_chief: print(' > %s:' % base_name)
    outputs = {}
    
    # Feed forward
    with tf.name_scope('net'):
        forward_pass(stage2_inputs, outputs, configuration, scope_name=base_name,
                     is_training=False, reuse=True, verbose=is_chief) 
            
    # Reshape outputs from stage2 to stage1
    # for summary
    unscaled_outputs = {key: outputs[key] for key in ['bounding_boxes', 'detection_scores']} 
    with tf.name_scope('reshape_outputs'):
        crop_boxes = stage1_outputs["crop_boxes"]  
        num_crops = crop_boxes.get_shape()[1].value
        num_boxes = outputs['bounding_boxes'].get_shape()[-2].value
        batch_size = graph_manager.get_defaults(configuration, ['test_batch_size'], verbose=is_chief)[0]
        # outputs:  (stage1_batch * num_crops, num_cell, num_cell, num_boxes, ...)
        # to: (stage1_batch, num_cell, num_cell, num_boxes * num_crops, ...)
        for key, value in outputs.items():
            shape = tf.shape(value)
            batches = tf.split(value, batch_size, axis=0)
            batches = [tf.concat(tf.unstack(b, num=num_crops, axis=0), axis=2) for b in batches]
            outputs[key] = tf.stack(batches, axis=0)
    
    # Rescale bounding boxes from stage2 to stage1
    with tf.name_scope('rescale_bounding_boxes'):
        # crop_boxes: (stage1_batch, 1, 1, num_crops * num_boxes, 4)
        crop_boxes = tf.reshape(crop_boxes, (batch_size, num_crops, 1, 4))
        crop_boxes = tf.tile(crop_boxes, (1, 1, num_boxes, 1))
        crop_boxes = tf.reshape(crop_boxes, (batch_size, 1, 1, num_crops * num_boxes, 4))
        crop_mins, crop_maxs = tf.split(crop_boxes, 2, axis=-1)
        # bounding_boxes: (stage1_batch, num_cells, num_cells, num_crops * num_boxes, 4)
        bounding_boxes = outputs['bounding_boxes']
        bounding_boxes *= tf.maximum(1e-8, tf.tile(crop_maxs - crop_mins, (1, 1, 1, 1, 2)))
        bounding_boxes += tf.tile(crop_mins, (1, 1, 1, 1, 2))
        bounding_boxes = tf.clip_by_value(bounding_boxes, 0., 1.)
        outputs['bounding_boxes'] = bounding_boxes
            
    ## Add the additional bounding boxes outputs propagated from earlier stages
    if 'added_bounding_boxes' in stage1_outputs:
        assert 'added_detection_scores' in stage1_outputs
        outputs['bounding_boxes'] = tf_utils.flatten_percell_output(outputs['bounding_boxes'])
        outputs['bounding_boxes'] = tf.concat([outputs['bounding_boxes'],
                                               stage1_outputs['added_bounding_boxes']], axis=1)
        outputs['detection_scores'] = tf_utils.flatten_percell_output(outputs['detection_scores'])
        outputs['detection_scores'] = tf.concat([outputs['detection_scores'],
                                                stage1_outputs['added_detection_scores']], axis=1)
        
    # Evaluate `output` versus the initial (stage 1) inputs
    with tf.name_scope('eval'):
        graph_manager.add_metrics_to_graph(
            eval_utils.get_standard_eval, stage1_inputs, outputs, metrics_to_norms, clear_metrics_op, 
            update_metrics_op, configuration, device=device, verbose=is_chief)
    return outputs, unscaled_outputs