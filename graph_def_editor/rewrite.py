# Copyright 2019 IBM. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""
rewrite.py

Graph rewrites that ship with the GraphDef Editor.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np
import tensorflow as tf
from typing import Tuple, Dict, Iterable, Union, Callable, Any

from graph_def_editor import graph, node, reroute, tensor, util

# Shorten select.TreeExpr to make patterns easier to read
from graph_def_editor.select import TreeExpr

__all__ = [
  "change_batch_size",
  "fold_batch_norms",
]


def change_batch_size(g: graph.Graph,
                      new_size: int,
                      inputs: Iterable[Union[node.Node, tensor.Tensor]]):
  """
  Change the batch size of a model.

  Runs size inference over the graph to propagate the new batch size
  throughout the graph.

  Modifies the graph in place. If the rewrite fails, the graph may be left
  in an inconsistent state.

  Args:
    g: The graph on which to modify the batch size. Modified in place.
    new_size: New batch size to apply on the input(s) to the graph.
      Can be `None` to indicate dynamic batch size.
    inputs: Placeholder nodes that are the input to the graph, either
      the `Node`s themselves or as their output `Tensor`s
  """
  input_nodes = [i.node if isinstance(i, tensor.Tensor) else i
                 for i in inputs]

  # Basic sanity checks
  for n in input_nodes:
    if n.op_type != "Placeholder":
      raise ValueError("Input node {} is not a Placeholder".format(n))
    if n.graph is not g:
      raise ValueError("Input node {} is not in graph {}".format(n, g))

  # Update input nodes
  for n in input_nodes:
    orig_shape = n.get_attr("shape")
    new_dims = [d for d in orig_shape.dims]
    new_dims[0] = new_size
    n.replace_attr("shape", tf.TensorShape(new_dims))

  # Propagate new batch size throughout graph
  g.infer_shapes_and_dtypes()


def _fixed_point_apply(pattern: TreeExpr,
                       action: Callable[[graph.Graph, Dict[str, node.Node]],
                                        bool],
                       g: graph.Graph):
  """
  Repeatedly apply a pattern-action rule until the graph stops changing.

  Args:
    pattern: Expression that selects a portion of the graph for modification
    action: Rule (as a Callable) that optionally modifies the graph. Returns
      True if modifications occurred and False otherwise.
  """
  keep_going = True
  while keep_going:
    keep_going = False
    # Each iteration walks through all the nodes of the graph to avoid O(n^2)
    # behavior
    nodes_before = g.nodes
    for n in nodes_before:
      if n.graph is None:
        # Node has been removed from the graph.
        continue
      match_info = pattern.eval_from(n)
      if match_info is not None:
        # Found a structural match rooted at the current node. Perform action.
        change_happened = action(g, match_info)
        if change_happened:
          keep_going = True


def _add_scale_to_conv_weights(conv_node: node.Node,
                               weights_node: node.Node,
                               scale: np.ndarray):
  """
  Subroutine of fold_batch_norms() and fold_old_batch_norms().

  Extract the weights from a Conv2D or MatMul op, multiply by scaling
  factors, and put the resulting scaled weights in place.

  Args:
    conv_node: Conv2D/MatMul node to be rewritten
    weights_node: Const node containing weights that parametrize the
      transformation that conv_node performs.
    scale: Array where each entry contains a scale factor for the
      corresponding output column of conv_node
  """
  if conv_node.op_type not in ("Conv2D", "MatMul"):
    raise ValueError("Unexpected op type {} for conv_node".format(
      conv_node.op_type))
  if weights_node.op_type != "Const":
    raise ValueError("Unexpected op type {} for weights_node".format(
      conv_node.op_type))

  weights = weights_node.get_attr("value")

  # Make sure all the inputs really are vectors, with as many entries as
  # there are columns in the weights.
  weights_cols_index = 3 if conv_node.op_type == "Conv2D" else 1
  weights_cols = weights.shape[weights_cols_index]
  if len(scale.shape) != 1 or scale.shape[0] != weights_cols:
    raise ValueError("Scale constant input to batch norm has bad shape: "
                     "{}".format(scale.shape))

  # Multiply the original weights by the scale vector.
  scaled_weights = weights.copy()
  for col in range(weights_cols):
    scaled_weights[..., col] *= scale[col]

  # Cast down to the original precision
  scaled_weights = scaled_weights.astype(weights.dtype)

  # Modify the node in place
  weights_node.replace_attr("value", scaled_weights)


def fold_batch_norms(g: graph.Graph):
  """
  Python port of the Graph Transform Tool rewrite by the same name.

  Identifies instances of the pattern `Conv2D => Mul` and folds the
  multiplication into the convolution's filter coefficients. This pattern
  occurs as a result of `Conv2D => BatchNorm` turning into
  `Conv2D => Mul => Add` when a multi-op batch normalization is used.
  """
  pattern = TreeExpr(op="Mul", alias="mul", inputs=(
    TreeExpr(op="Conv2D|MatMul", alias="conv", inputs=(
      TreeExpr(),
      TreeExpr(op="Const", alias="weights")
    )),
    TreeExpr(op="Const", alias="mul_values")))

  def action(_, match_info: Dict[str, node.Node]) -> bool:
    mul_node = match_info["mul"]
    conv_node = match_info["conv"]
    weights_node = match_info["weights"]
    mul_values_node = match_info["mul_values"]

    mul_values = mul_values_node.get_attr("value")

    # If there is another direct consumer of the output of the convolution,
    # skip the rewrite.
    if len(conv_node.outputs[0].consumers()) > 1:
      return False

    _add_scale_to_conv_weights(conv_node, weights_node, mul_values)

    # Cut the Mul node out of the graph
    reroute.reroute_ts(mul_node.outputs[0], mul_node.inputs[0])
    g.remove_node_by_name(mul_node.name)

    # Const might still be in use; check before removing it.
    if len(mul_values_node.outputs[0].consumers()) == 0:
      g.remove_node_by_name(mul_values_node.name)

    # Original rewrite gave the name of the Mul node to the Conv2D. Recreate
    # that behavior here, including putting the node in the collections that
    # the Mul node was a member of.
    g.rename_node(conv_node.name, mul_node.name)
    conv_node.remove_from_collections()
    for collection_name in mul_node.collection_names:
      conv_node.add_to_collection(collection_name)
    return True

  _fixed_point_apply(pattern, action, g)


def fold_old_batch_norms(g: graph.Graph):
  """
  Python port of the Graph Transform Tool rewrite by the same name.

  This rewrite looks for instances of the pattern `Conv2D => [batch norm]`,
  where [batch norm] is a fused batch normalization operator.

  The TF documentation says that this rewrite is only for graphs produced by
  legacy code, but this is not true. As of January 2019, the most recent
  version of TensorFlow produces fused batch normalization operators by default.

  Specifically, legacy code uses the `BatchNormWithGlobalNormalization` op,
  while new code uses the `FusedBatchNorm` op.

  In addition to covering the basic `Conv2D => [batch norm]` pattern,
  the rewrite also covers the cases where some postprocessing nodes exist
  between the `Conv2D` and the `[batch norm]` parts. As a result, the rewrite
  proceeds in three passes.
  """
  # Start by defining some useful subroutines.
  def get_batch_norm_params(
          batch_norm_node: node.Node) -> Tuple[np.ndarray, np.ndarray,
                                               np.ndarray, np.ndarray,
                                               Any, bool]:
    """
    Delve into the inputs of a fused batch normalization node and fetch the
    constant values for the descriptive statistics that define the
    normalization.

    Args:
      batch_norm_node: The fused batch normalization op. The caller is
      responsible for ensuring that the variable inputs to this op have been
      converted to consts.

    Returns:
      The following values:
        mean, variance, beta, gamma, variance_epsilon,
        scale_after_normalization
      The first four return values contain descriptive stats, cast to float64,
      while the last is a Boolean value.
    """
    def get_const_values():
      return (np.float64(batch_norm_node.inputs[ix].node.get_attr("value"))
              for ix in range(1, 5))

    # Compensate for different input orders and attribute names
    if "BatchNormWithGlobalNormalization" == batch_norm_node.op_type:
      mean, variance, beta, gamma = get_const_values()
      variance_epsilon = np.float64(batch_norm_node.get_attr(
        "variance_epsilon"))
      scale_after_normalization = bool(batch_norm_node.get_attr(
        "scale_after_normalization"))
    elif "FusedBatchNorm" == batch_norm_node.op_type:
      gamma, beta, mean, variance = get_const_values()
      variance_epsilon = np.float64(batch_norm_node.get_attr("epsilon"))
      scale_after_normalization = True
    else:
      raise ValueError("Unexpected op type {} for fused batch norm".format(
        batch_norm_node.op_type))
    return (mean, variance, beta, gamma, variance_epsilon,
            scale_after_normalization)

  def get_scale_and_offset(match_info: Dict[str, node.Node]):
    """
    Dig the batch normalization parameters out of a subgraph match and
    compute scale and offset vectors that the normalization applies at
    inference time.

    Args:
      match_info: Should contain ops under the following keys:
        "batch_norm" ==> fused batch normalization op
        "mean", "variance", "beta", "gamma" ==> Const nodes containing
          normalization parameters

    Returns:
      scale, offset: Two Numpy arrays containing the scale and offset vectors
        of 64-bit floating point numbers
    """
    batch_norm_node = match_info["batch_norm"]
    (mean, variance, beta, gamma, variance_epsilon,
     scale_after_normalization) = get_batch_norm_params(batch_norm_node)

    # Sanity check: Everything should have the same 1-D shape
    mean_shape = mean.shape
    if len(mean_shape) != 1:
      raise ValueError("Shape of mean ({}) is not a vector".format(mean_shape))
    if (variance.shape != mean_shape or beta.shape != mean_shape or
            gamma.shape != mean_shape):
      raise ValueError("Shapes {}, {}, {}, and {} for mean, variance, beta, "
                       "and gamma don't all match"
                       "".format(mean.shape, variance.shape, beta.shape,
                                 gamma.shape))

    # Now we have everything we need to compute scale and offset values.
    if scale_after_normalization:
      scale = (1.0 / np.sqrt(variance + variance_epsilon)) * gamma
    else:
      scale = (1.0 / np.sqrt(variance + variance_epsilon))
    offset = (-mean * scale) + beta

    return scale, offset

  def build_bias_add(node_name: str, t_attr: tf.AttrValue,
                     offset: np.ndarray, data_format: str,
                     input_tensor: tensor.Tensor,
                     output_dtype: tf.DType,
                     output_shape: tf.TensorShape):
    # TODO(frreiss): Support non-32-bit offsets
    bias_offset_node = util.make_const(g, node_name + "_offset",
                                       np.float32(offset), uniquify_name=True)
    bias_add_node = g.add_node(node_name, "BiasAdd", uniquify_name=True)
    if data_format is not None:
      bias_add_node.add_attr("data_format", data_format)
    bias_add_node.add_attr("T", t_attr)
    bias_add_node.set_inputs([input_tensor, bias_offset_node])
    bias_add_node.set_outputs_from_pairs([(output_dtype, output_shape)])
    return bias_add_node

  def replace_batch_norm_with_bias_add(match_info: Dict[str, node.Node],
                                       offset: np.ndarray):
    """
    Replace the fused batch normalization node in the graph with a BiasAdd
    node that applies the offset from the original normalization.
    Then remove the batch normalization node and its input constants.

    Args:
      match_info: Should contain ops under the following keys:
        "batch_norm" ==> fused batch normalization op
        "conv" ==> Convolution or matmul op that feeds into the batch
          normalization
        "mean", "variance", "beta", "gamma" ==> Const nodes containing
          normalization parameters
      offset: Offset that the batch norm node applies at inference time
    """
    batch_norm_node = match_info["batch_norm"]
    orig_inputs = batch_norm_node.inputs
    conv_node = match_info["conv"] if "conv" in match_info else match_info[
      "conv0"]
    data_format = conv_node.get_attr("data_format") if conv_node.has_attr(
      "data_format") else None

    bias_add_node = build_bias_add(batch_norm_node.name + "_bias_add",
                                   batch_norm_node.get_attr("T"),
                                   offset, data_format,
                                   batch_norm_node.inputs[0],
                                   batch_norm_node.output(0).dtype,
                                   batch_norm_node.output(0).shape)

    # Splice the batch norm op out of the graph and replace with a newly
    # created BiasAdd node.
    # Note that the batch norm node has a bunch of other outputs that aren't
    # used in inference.
    reroute.reroute_ts(batch_norm_node.output(0), bias_add_node.outputs)
    g.remove_node_by_name(batch_norm_node.name)

    # Original rewrite gave the name of the batch norm node to the BiasAdd.
    # Recreate that behavior here, including putting the node in the
    # collections that the original node was a member of.
    g.rename_node(bias_add_node.name, batch_norm_node.name)
    for collection_name in batch_norm_node.collection_names:
      bias_add_node.add_to_collection(collection_name)

    # Remove the input constants if they are no longer used.
    for ix in range(1, 5):
      in_tensor = orig_inputs[ix]
      if len(in_tensor.consumers()) == 0:
        g.remove_node_by_name(in_tensor.node.name)

  # Now we perform three passes to cover three different types of subgraph.
  # PASS 1: Simple Conv2D => [batch norm] pattern.
  pattern_1 = TreeExpr(
    op="BatchNormWithGlobalNormalization|FusedBatchNorm",
    alias="batch_norm", inputs=(
      TreeExpr(op="Conv2D", alias="conv", inputs=(
          TreeExpr(),
          TreeExpr(op="Const", alias="weights")
        )),
      TreeExpr(op="Const"),
      TreeExpr(op="Const"),
      TreeExpr(op="Const"),
      TreeExpr(op="Const"),
    ))

  def action_1(_, match_info: Dict[str, node.Node]) -> bool:
    conv_node = match_info["conv"]
    weights_node = match_info["weights"]

    # If there is another direct consumer of the output of the convolution,
    # skip the rewrite.
    if len(conv_node.outputs[0].consumers()) > 1:
      return False

    scale, offset = get_scale_and_offset(match_info)
    _add_scale_to_conv_weights(conv_node, weights_node, scale)
    replace_batch_norm_with_bias_add(match_info, offset)
    return True

  _fixed_point_apply(pattern_1, action_1, g)

  # PASS 2: Conv2D => BatchToSpaceND => [batch norm]
  pattern_2 = TreeExpr(
    op="BatchNormWithGlobalNormalization|FusedBatchNorm",
    alias="batch_norm", inputs=(
      TreeExpr(op="BatchToSpaceND", alias="batch_to_space", inputs=(
        TreeExpr(op="Conv2D", alias="conv", inputs=(
          TreeExpr(),
          TreeExpr(op="Const", alias="weights")
        )))),
      TreeExpr(op="Const"),
      TreeExpr(op="Const"),
      TreeExpr(op="Const"),
      TreeExpr(op="Const"),
    ))

  def action_2(_, match_info: Dict[str, node.Node]) -> bool:
    conv_node = match_info["conv"]
    weights_node = match_info["weights"]

    # If there is another direct consumer of the output of the convolution,
    # the BatchToSpaceND, or the convolution weights, skip the rewrite
    for n in (conv_node, weights_node, match_info["batch_to_space"]):
      if len(n.output(0).consumers()) > 1:
        return False

    scale, offset = get_scale_and_offset(match_info)
    _add_scale_to_conv_weights(conv_node, weights_node, scale)
    replace_batch_norm_with_bias_add(match_info, offset)
    return True

  _fixed_point_apply(pattern_2, action_2, g)

  # PASS 3: Two Conv2D's -> Concat -> [batch norm]
  pattern_3 = TreeExpr(
    op="BatchNormWithGlobalNormalization|FusedBatchNorm",
    alias="batch_norm", inputs=(
      TreeExpr(op="ConcatV2|Concat", alias="concat", inputs=(
        TreeExpr(op="Conv2D", alias="conv0", inputs=(
          TreeExpr(),
          TreeExpr(op="Const", alias="weights0")
        )),
        TreeExpr(op="Conv2D", alias="conv1", inputs=(
          TreeExpr(),
          TreeExpr(op="Const", alias="weights1")
        )),
        TreeExpr(op="Const", alias="axis")
      )),
      TreeExpr(op="Const"),
      TreeExpr(op="Const"),
      TreeExpr(op="Const"),
      TreeExpr(op="Const"),
    ))

  def action_3(_, match_info: Dict[str, node.Node]) -> bool:
    # If there is another direct consumer of anything between a conv and the
    # final output, skip the rewrite
    if len(match_info["conv0"].outputs[0].consumers()) > 1:
      return False
    if len(match_info["conv1"].outputs[0].consumers()) > 1:
      return False
    if len(match_info["concat"].outputs[0].consumers()) > 1:
      return False

    conv0_node = match_info["conv0"]
    conv1_node = match_info["conv1"]
    weights0_node = match_info["weights0"]
    weights1_node = match_info["weights1"]

    scale, offset = get_scale_and_offset(match_info)

    axis = match_info["axis"].get_attr("value")
    if axis == 3:
      # Concatenating along channel axis ==> Need to split scale and offset
      split_cols = weights0_node.get_attr("value").shape[3]
      scale_0, offset_0 = scale[:split_cols], offset[:split_cols]
      scale_1, offset_1 = scale[split_cols:], offset[split_cols:]
    else:
      # Concatenating along axis other than channel ==> Scale every channel
      scale_0, offset_0 = scale, offset
      scale_1, offset_1 = scale, offset

    _add_scale_to_conv_weights(conv0_node, weights0_node, scale_0)
    _add_scale_to_conv_weights(conv1_node, weights1_node, scale_1)

    replace_batch_norm_with_bias_add(match_info, offset)
    return True

  _fixed_point_apply(pattern_3, action_3, g)
