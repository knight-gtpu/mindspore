# Copyright 2020 Huawei Technologies Co., Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Image Classification Runner."""
import os
import re
from time import time

import numpy as np
from PIL import Image

import mindspore as ms
import mindspore.dataset as ds
from mindspore import log
from mindspore.dataset.engine.datasets import Dataset
from mindspore.nn import Cell, SequentialCell
from mindspore.ops.operations import ExpandDims
from mindspore.train._utils import check_value_type
from mindspore.train.summary._summary_adapter import _convert_image_format
from mindspore.train.summary.summary_record import SummaryRecord
from mindspore.train.summary_pb2 import Explain
from .benchmark import Localization
from .explanation import RISE
from .benchmark._attribution.metric import AttributionMetric, LabelSensitiveMetric, LabelAgnosticMetric
from .explanation._attribution.attribution import Attribution

_EXPAND_DIMS = ExpandDims()


def _normalize(img_np):
    """Normalize the numpy image to the range of [0, 1]. """
    max_ = img_np.max()
    min_ = img_np.min()
    normed = (img_np - min_) / (max_ - min_).clip(min=1e-10)
    return normed


def _np_to_image(img_np, mode):
    """Convert numpy array to PIL image."""
    return Image.fromarray(np.uint8(img_np * 255), mode=mode)


class ImageClassificationRunner:
    """
    A high-level API for users to generate and store results of the explanation methods and the evaluation methods.

    Update in 2020.11: Adjust the storage structure and format of the data. Summary files generated by previous version
    will be deprecated and will not be supported in MindInsight of current version.

    Args:
        summary_dir (str): The directory path to save the summary files which store the generated results.
        data (tuple[Dataset, list[str]]): Tuple of dataset and the corresponding class label list. The dataset
            should provides [images], [images, labels] or [images, labels, bboxes] as columns. The label list must
            share the exact same length and order of the network outputs.
        network (Cell): The network(with logit outputs) to be explained.
        activation_fn (Cell): The activation layer that transforms logits to prediction probabilities. For
            single label classification tasks, `nn.Softmax` is usually applied. As for multi-label classification tasks,
            `nn.Sigmoid` is usually be applied. Users can also pass their own customized `activation_fn` as long as
            when combining this function with network, the final output is the probability of the input.

    Examples:
        >>> from mindspore.explainer import ImageClassificationRunner
        >>> from mindspore.explainer.explanation import GuidedBackprop, Gradient
        >>> from mindspore.explainer.benchmark import Faithfulness
        >>> from mindspore.nn import Softmax
        >>> from mindspore.train.serialization import load_checkpoint, load_param_into_net
        >>> # Prepare the dataset for explaining and evaluation, e.g., Cifar10
        >>> dataset = get_dataset('/path/to/Cifar10_dataset')
        >>> labels = ['airplane', 'automobile', 'bird', 'cat', 'deer', 'dog', 'frog', 'horse', 'ship', 'turck']
        >>> # load checkpoint to a network, e.g. checkpoint of resnet50 trained on Cifar10
        >>> param_dict = load_checkpoint("checkpoint.ckpt")
        >>> net = resnet50(len(classes))
        >>> activation_fn = Softmax()
        >>> load_param_into_net(net, param_dict)
        >>> gbp = GuidedBackprop(net)
        >>> gradient = Gradient(net)
        >>> explainers = [gbp, gradient]
        >>> faithfulness = Faithfulness(len(labels), "NaiveFaithfulness", activation_fn)
        >>> benchmarkers = [faithfulness]
        >>> runner = ImageClassificationRunner("./summary_dir", (dataset, labels), net, activation_fn)
        >>> runner.register_saliency(explainers=explainers, benchmarkers=benchmarkers)
        >>> runner.run()
    """

    # datafile directory names
    _DATAFILE_DIRNAME_PREFIX = "_explain_"
    _ORIGINAL_IMAGE_DIRNAME = "origin_images"
    _HEATMAP_DIRNAME = "heatmap"
    # max. no. of sample per directory
    _SAMPLE_PER_DIR = 1000
    # seed for fixing the iterating order of the dataset
    _DATASET_SEED = 58
    # printing spacer
    _SPACER = "{:120}\r"
    # datafile directory's permission
    _DIR_MODE = 0o750
    # datafile's permission
    _FILE_MODE = 0o600

    def __init__(self,
                 summary_dir,
                 data,
                 network,
                 activation_fn):

        check_value_type("data", data, tuple)
        if len(data) != 2:
            raise ValueError("Argument data is not a tuple with 2 elements")
        check_value_type("data[0]", data[0], Dataset)
        check_value_type("data[1]", data[1], list)
        if not all(isinstance(ele, str) for ele in data[1]):
            raise ValueError("Argument data[1] is not list of str.")

        check_value_type("summary_dir", summary_dir, str)
        check_value_type("network", network, Cell)
        check_value_type("activation_fn", activation_fn, Cell)

        self._summary_dir = summary_dir
        self._dataset = data[0]
        self._labels = data[1]
        self._network = network
        self._explainers = None
        self._benchmarkers = None
        self._summary_timestamp = None
        self._sample_index = -1

        self._full_network = SequentialCell([self._network, activation_fn])

        self._verify_data_n_settings(check_data_n_network=True)

    def register_saliency(self,
                          explainers,
                          benchmarkers=None):
        """
        Register saliency explanation instances.

        Note:
            This function call not be invoked more then once on each runner.

        Args:
            explainers (list[Attribution]): The explainers to be evaluated,
                see `mindspore.explainer.explanation`. All explainers' class must be distinct and their network
                must be the exact same instance of the runner's network.
            benchmarkers (list[AttributionMetric], optional): The benchmarkers for scoring the explainers,
                see `mindspore.explainer.benchmark`. All benchmarkers' class must be distinct.

        Raises:
            ValueError: Be raised for any data or settings' value problem.
            TypeError: Be raised for any data or settings' type problem.
            RuntimeError: Be raised if this function was invoked before.
        """
        check_value_type("explainers", explainers, list)
        if not all(isinstance(ele, Attribution) for ele in explainers):
            raise TypeError("Argument explainers is not list of mindspore.explainer.explanation .")

        if not explainers:
            raise ValueError("Argument explainers is empty.")

        if benchmarkers:
            check_value_type("benchmarkers", benchmarkers, list)
            if not all(isinstance(ele, AttributionMetric) for ele in benchmarkers):
                raise TypeError("Argument benchmarkers is not list of mindspore.explainer.benchmark .")

        if self._explainers is not None:
            raise RuntimeError("Function register_saliency() was invoked already.")

        self._explainers = explainers
        self._benchmarkers = benchmarkers

        try:
            self._verify_data_n_settings(check_saliency=True)
        except (ValueError, TypeError):
            self._explainers = None
            self._benchmarkers = None
            raise

    def run(self):
        """
        Run the explain job and save the result as a summary in summary_dir.

        Note:
            User should call register_saliency() once before running this function.

        Raises:
            ValueError: Be raised for any data or settings' value problem.
            TypeError: Be raised for any data or settings' type problem.
            RuntimeError: Be raised for any runtime problem.
        """
        self._verify_data_n_settings(check_all=True)

        with SummaryRecord(self._summary_dir) as summary:
            print("Start running and writing......")
            begin = time()

            self._summary_timestamp = self._extract_timestamp(summary.event_file_name)
            if self._summary_timestamp is None:
                raise RuntimeError("Cannot extract timestamp from summary filename!"
                                   " It should contains a timestamp after 'summary.' .")

            self._save_metadata(summary)

            imageid_labels = self._run_inference(summary)
            if self._is_saliency_registered:
                self._run_saliency(summary, imageid_labels)

            print("Finish running and writing. Total time elapsed: {:.3f} s".format(time() - begin))

    @property
    def _is_saliency_registered(self):
        """Check if saliency module is registered."""
        return bool(self._explainers)

    def _save_metadata(self, summary):
        """Save metadata of the explain job to summary."""
        print("Start writing metadata......")

        explain = Explain()
        explain.metadata.label.extend(self._labels)

        if self._is_saliency_registered:
            exp_names = [exp.__class__.__name__ for exp in self._explainers]
            explain.metadata.explain_method.extend(exp_names)
            if self._benchmarkers is not None:
                bench_names = [bench.__class__.__name__ for bench in self._benchmarkers]
                explain.metadata.benchmark_method.extend(bench_names)

        summary.add_value("explainer", "metadata", explain)
        summary.record(1)

        print("Finish writing metadata.")

    def _run_inference(self, summary, threshold=0.5):
        """
        Run inference for the dataset and write the inference related data into summary.

        Args:
            summary (SummaryRecord): The summary object to store the data
            threshold (float): The threshold for prediction.

        Returns:
            dict, The map of sample d to the union of its ground truth and predicted labels.
        """
        sample_id_labels = {}
        self._sample_index = 0
        ds.config.set_seed(self._DATASET_SEED)
        for j, next_element in enumerate(self._dataset):
            now = time()
            inputs, labels, _ = self._unpack_next_element(next_element)
            prob = self._full_network(inputs).asnumpy()

            for idx, inp in enumerate(inputs):
                gt_labels = labels[idx]
                gt_probs = [float(prob[idx][i]) for i in gt_labels]

                data_np = _convert_image_format(np.expand_dims(inp.asnumpy(), 0), 'NCHW')
                original_image = _np_to_image(_normalize(data_np), mode='RGB')
                original_image_path = self._save_original_image(self._sample_index, original_image)

                predicted_labels = [int(i) for i in (prob[idx] > threshold).nonzero()[0]]
                predicted_probs = [float(prob[idx][i]) for i in predicted_labels]

                union_labs = list(set(gt_labels + predicted_labels))
                sample_id_labels[str(self._sample_index)] = union_labs

                explain = Explain()
                explain.sample_id = self._sample_index
                explain.image_path = original_image_path
                summary.add_value("explainer", "sample", explain)

                explain = Explain()
                explain.sample_id = self._sample_index
                explain.ground_truth_label.extend(gt_labels)
                explain.inference.ground_truth_prob.extend(gt_probs)
                explain.inference.predicted_label.extend(predicted_labels)
                explain.inference.predicted_prob.extend(predicted_probs)

                summary.add_value("explainer", "inference", explain)

                summary.record(1)

                self._sample_index += 1
            self._spaced_print("Finish running and writing {}-th batch inference data."
                               " Time elapsed: {:.3f} s".format(j, time() - now),
                               end='')
        return sample_id_labels

    def _run_saliency(self, summary, sample_id_labels):
        """Run the saliency explanations."""
        if self._benchmarkers is None or not self._benchmarkers:
            for exp in self._explainers:
                start = time()
                print("Start running and writing explanation data for {}......".format(exp.__class__.__name__))
                self._sample_index = 0
                ds.config.set_seed(self._DATASET_SEED)
                for idx, next_element in enumerate(self._dataset):
                    now = time()
                    self._spaced_print("Start running {}-th explanation data for {}......".format(
                        idx, exp.__class__.__name__), end='')
                    self._run_exp_step(next_element, exp, sample_id_labels, summary)
                    self._spaced_print("Finish writing {}-th explanation data for {}. Time elapsed: "
                                       "{:.3f} s".format(idx, exp.__class__.__name__, time() - now), end='')
                self._spaced_print(
                    "Finish running and writing explanation data for {}. Time elapsed: {:.3f} s".format(
                        exp.__class__.__name__, time() - start))
        else:
            for exp in self._explainers:
                explain = Explain()
                for bench in self._benchmarkers:
                    bench.reset()
                print(f"Start running and writing explanation and "
                      f"benchmark data for {exp.__class__.__name__}......")
                self._sample_index = 0
                start = time()
                ds.config.set_seed(self._DATASET_SEED)
                for idx, next_element in enumerate(self._dataset):
                    now = time()
                    self._spaced_print("Start running {}-th explanation data for {}......".format(
                        idx, exp.__class__.__name__), end='')
                    saliency_dict_lst = self._run_exp_step(next_element, exp, sample_id_labels, summary)
                    self._spaced_print(
                        "Finish writing {}-th batch explanation data for {}. Time elapsed: {:.3f} s".format(
                            idx, exp.__class__.__name__, time() - now), end='')
                    for bench in self._benchmarkers:
                        now = time()
                        self._spaced_print(
                            "Start running {}-th batch {} data for {}......".format(
                                idx, bench.__class__.__name__, exp.__class__.__name__), end='')
                        self._run_exp_benchmark_step(next_element, exp, bench, saliency_dict_lst)
                        self._spaced_print(
                            "Finish running {}-th batch {} data for {}. Time elapsed: {:.3f} s".format(
                                idx, bench.__class__.__name__, exp.__class__.__name__, time() - now), end='')

                for bench in self._benchmarkers:
                    benchmark = explain.benchmark.add()
                    benchmark.explain_method = exp.__class__.__name__
                    benchmark.benchmark_method = bench.__class__.__name__

                    benchmark.total_score = bench.performance
                    if isinstance(bench, LabelSensitiveMetric):
                        benchmark.label_score.extend(bench.class_performances)

                self._spaced_print("Finish running and writing explanation and benchmark data for {}. "
                                   "Time elapsed: {:.3f} s".format(exp.__class__.__name__, time() - start))
                summary.add_value('explainer', 'benchmark', explain)
                summary.record(1)

    def _run_exp_step(self, next_element, explainer, sample_id_labels, summary):
        """
        Run the explanation for each step and write explanation results into summary.

        Args:
            next_element (Tuple): Data of one step
            explainer (_Attribution): An Attribution object to generate saliency maps.
            sample_id_labels (dict): A dict that maps the sample id and its union labels.
            summary (SummaryRecord): The summary object to store the data

        Returns:
            list, List of dict that maps label to its corresponding saliency map.
        """
        inputs, labels, _ = self._unpack_next_element(next_element)
        sample_index = self._sample_index
        unions = []
        for _ in range(len(labels)):
            unions_labels = sample_id_labels[str(sample_index)]
            unions.append(unions_labels)
            sample_index += 1

        batch_unions = self._make_label_batch(unions)
        saliency_dict_lst = []

        if isinstance(explainer, RISE):
            batch_saliency_full = explainer(inputs, batch_unions)
        else:
            batch_saliency_full = []
            for i in range(len(batch_unions[0])):
                batch_saliency = explainer(inputs, batch_unions[:, i])
                batch_saliency_full.append(batch_saliency)
            concat = ms.ops.operations.Concat(1)
            batch_saliency_full = concat(tuple(batch_saliency_full))

        for idx, union in enumerate(unions):
            saliency_dict = {}
            explain = Explain()
            explain.sample_id = self._sample_index
            for k, lab in enumerate(union):
                saliency = batch_saliency_full[idx:idx + 1, k:k + 1]
                saliency_dict[lab] = saliency

                saliency_np = _normalize(saliency.asnumpy().squeeze())
                saliency_image = _np_to_image(saliency_np, mode='L')
                heatmap_path = self._save_heatmap(explainer.__class__.__name__, lab, self._sample_index, saliency_image)

                explanation = explain.explanation.add()
                explanation.explain_method = explainer.__class__.__name__
                explanation.heatmap_path = heatmap_path
                explanation.label = lab

            summary.add_value("explainer", "explanation", explain)
            summary.record(1)

            self._sample_index += 1
            saliency_dict_lst.append(saliency_dict)
        return saliency_dict_lst

    def _run_exp_benchmark_step(self, next_element, explainer, benchmarker, saliency_dict_lst):
        """Run the explanation and evaluation for each step and write explanation results into summary."""
        inputs, labels, _ = self._unpack_next_element(next_element)
        for idx, inp in enumerate(inputs):
            inp = _EXPAND_DIMS(inp, 0)
            saliency_dict = saliency_dict_lst[idx]
            for label, saliency in saliency_dict.items():
                if isinstance(benchmarker, Localization):
                    _, _, bboxes = self._unpack_next_element(next_element, True)
                    if label in labels[idx]:
                        res = benchmarker.evaluate(explainer, inp, targets=label, mask=bboxes[idx][label],
                                                   saliency=saliency)
                        if np.any(res == np.nan):
                            res = np.zeros_like(res)
                        benchmarker.aggregate(res, label)
                elif isinstance(benchmarker, LabelSensitiveMetric):
                    res = benchmarker.evaluate(explainer, inp, targets=label, saliency=saliency)
                    if np.any(res == np.nan):
                        res = np.zeros_like(res)
                    benchmarker.aggregate(res, label)
                elif isinstance(benchmarker, LabelAgnosticMetric):
                    res = benchmarker.evaluate(explainer, inp)
                    if np.any(res == np.nan):
                        res = np.zeros_like(res)
                    benchmarker.aggregate(res)
                else:
                    raise TypeError('Benchmarker must be one of LabelSensitiveMetric or LabelAgnosticMetric, but'
                                    'receive {}'.format(type(benchmarker)))

    def _verify_data(self):
        """Verify dataset and labels."""
        next_element = next(self._dataset.create_tuple_iterator())

        if len(next_element) not in [1, 2, 3]:
            raise ValueError("The dataset should provide [images] or [images, labels], [images, labels, bboxes]"
                             " as columns.")

        if len(next_element) == 3:
            inputs, labels, bboxes = next_element
            if bboxes.shape[-1] != 4:
                raise ValueError("The third element of dataset should be bounding boxes with shape of "
                                 "[batch_size, num_ground_truth, 4].")
        else:
            if self._benchmarkers is not None:
                if any([isinstance(bench, Localization) for bench in self._benchmarkers]):
                    raise ValueError("The dataset must provide bboxes if Localization is to be computed.")

            if len(next_element) == 2:
                inputs, labels = next_element
            if len(next_element) == 1:
                inputs = next_element[0]

        if len(inputs.shape) > 4 or len(inputs.shape) < 3 or inputs.shape[-3] not in [1, 3, 4]:
            raise ValueError(
                "Image shape {} is unrecognizable: the dimension of image can only be CHW or NCHW.".format(
                    inputs.shape))
        if len(inputs.shape) == 3:
            log.warning(
                "Image shape {} is 3-dimensional. All the data will be automatically unsqueezed at the 0-th"
                " dimension as batch data.".format(inputs.shape))
        if len(next_element) > 1:
            if len(labels.shape) > 2 and (np.array(labels.shape[1:]) > 1).sum() > 1:
                raise ValueError(
                    "Labels shape {} is unrecognizable: outputs should not have more than two dimensions"
                    " with length greater than 1.".format(labels.shape))

    def _verify_network(self):
        """Verify the network."""
        label_set = set()
        for i, label in enumerate(self._labels):
            if label.strip() == "":
                raise ValueError(f"Label [{i}] is all whitespaces or empty. Please make sure there is "
                                 f"no empty label.")
            if label in label_set:
                raise ValueError(f"Duplicated label:{label}! Please make sure all labels are unique.")
            label_set.add(label)

        next_element = next(self._dataset.create_tuple_iterator())
        inputs, _, _ = self._unpack_next_element(next_element)
        prop_test = self._full_network(inputs)
        check_value_type("output of network in explainer", prop_test, ms.Tensor)
        if prop_test.shape[1] != len(self._labels):
            raise ValueError("The dimension of network output does not match the no. of classes. Please "
                             "check labels or the network in the explainer again.")

    def _verify_saliency(self):
        """Verify the saliency settings."""
        if self._explainers:
            explainer_classes = []
            for explainer in self._explainers:
                if explainer.__class__ in explainer_classes:
                    raise ValueError(f"Repeated {explainer.__class__.__name__} explainer! "
                                     "Please make sure all explainers' class is distinct.")
                if explainer.network is not self._network:
                    raise ValueError(f"The network of {explainer.__class__.__name__} explainer is different "
                                     "instance from network of runner. Please make sure they are the same "
                                     "instance.")
                explainer_classes.append(explainer.__class__)
        if self._benchmarkers:
            benchmarker_classes = []
            for benchmarker in self._benchmarkers:
                if benchmarker.__class__ in benchmarker_classes:
                    raise ValueError(f"Repeated {benchmarker.__class__.__name__} benchmarker! "
                                     "Please make sure all benchmarkers' class is distinct.")
                if isinstance(benchmarker, LabelSensitiveMetric) and benchmarker.num_labels != len(self._labels):
                    raise ValueError(f"The num_labels of {benchmarker.__class__.__name__} benchmarker is different "
                                     "from no. of labels of runner. Please make them are the same.")
                benchmarker_classes.append(benchmarker.__class__)

    def _verify_data_n_settings(self,
                                check_all=False,
                                check_registration=False,
                                check_data_n_network=False,
                                check_saliency=False):
        """
        Verify the validity of dataset and other settings.

        Args:
            check_all (bool): Set it True for checking everything.
            check_registration (bool): Set it True for checking registrations, check if it is enough to invoke run().
            check_data_n_network (bool): Set it True for checking data and network.
            check_saliency (bool): Set it True for checking saliency related settings.

        Raises:
            ValueError: Be raised for any data or settings' value problem.
            TypeError: Be raised for any data or settings' type problem.
        """
        if check_all:
            check_registration = True
            check_data_n_network = True
            check_saliency = True

        if check_registration:
            if not self._is_saliency_registered:
                raise ValueError("No explanation module was registered, user should at least call register_saliency()"
                                 " once with proper explanation instances")

        if check_data_n_network or check_saliency:
            self._verify_data()

        if check_data_n_network:
            self._verify_network()

        if check_saliency:
            self._verify_saliency()

    def _transform_data(self, inputs, labels, bboxes, ifbbox):
        """
        Transform the data from one iteration of dataset to a unifying form for the follow-up operations.

        Args:
            inputs (Tensor): the image data
            labels (Tensor): the labels
            bboxes (Tensor): the boudnding boxes data
            ifbbox (bool): whether to preprocess bboxes. If True, a dictionary that indicates bounding boxes w.r.t
                label id will be returned. If False, the returned bboxes is the the parsed bboxes.

        Returns:
            inputs (Tensor): the image data, unified to a 4D Tensor.
            labels (list[list[int]]): the ground truth labels.
            bboxes (Union[list[dict], None, Tensor]): the bounding boxes
        """
        inputs = ms.Tensor(inputs, ms.float32)
        if len(inputs.shape) == 3:
            inputs = _EXPAND_DIMS(inputs, 0)
            if isinstance(labels, ms.Tensor):
                labels = ms.Tensor(labels, ms.int32)
                labels = _EXPAND_DIMS(labels, 0)
            if isinstance(bboxes, ms.Tensor):
                bboxes = ms.Tensor(bboxes, ms.int32)
                bboxes = _EXPAND_DIMS(bboxes, 0)

        input_len = len(inputs)
        if bboxes is not None and ifbbox:
            bboxes = ms.Tensor(bboxes, ms.int32)
            masks_lst = []
            labels = labels.asnumpy().reshape([input_len, -1])
            bboxes = bboxes.asnumpy().reshape([input_len, -1, 4])
            for idx, label in enumerate(labels):
                height, width = inputs[idx].shape[-2], inputs[idx].shape[-1]
                masks = {}
                for j, label_item in enumerate(label):
                    target = int(label_item)
                    if -1 < target < len(self._labels):
                        if target not in masks:
                            mask = np.zeros((1, 1, height, width))
                        else:
                            mask = masks[target]
                        x_min, y_min, x_len, y_len = bboxes[idx][j].astype(int)
                        mask[:, :, x_min:x_min + x_len, y_min:y_min + y_len] = 1
                        masks[target] = mask

                masks_lst.append(masks)
            bboxes = masks_lst

        labels = ms.Tensor(labels, ms.int32)
        if len(labels.shape) == 1:
            labels_lst = [[int(i)] for i in labels.asnumpy()]
        else:
            labels = labels.asnumpy().reshape([input_len, -1])
            labels_lst = []
            for item in labels:
                labels_lst.append(list(set(int(i) for i in item if -1 < int(i) < len(self._labels))))
        labels = labels_lst
        return inputs, labels, bboxes

    def _unpack_next_element(self, next_element, ifbbox=False):
        """
        Unpack a single iteration of dataset.

        Args:
            next_element (Tuple): a single element iterated from dataset object.
            ifbbox (bool): whether to preprocess bboxes in self._transform_data.

        Returns:
            tuple, a unified Tuple contains image_data, labels, and bounding boxes.
        """
        if len(next_element) == 3:
            inputs, labels, bboxes = next_element
        elif len(next_element) == 2:
            inputs, labels = next_element
            bboxes = None
        else:
            inputs = next_element[0]
            labels = [[] for _ in inputs]
            bboxes = None
        inputs, labels, bboxes = self._transform_data(inputs, labels, bboxes, ifbbox)
        return inputs, labels, bboxes

    @staticmethod
    def _make_label_batch(labels):
        """
        Unify a List of List of labels to be a 2D Tensor with shape (b, m), where b = len(labels) and m is the max
        length of all the rows in labels.

        Args:
            labels (List[List]): the union labels of a data batch.

        Returns:
            2D Tensor.
        """
        max_len = max([len(label) for label in labels])
        batch_labels = np.zeros((len(labels), max_len))

        for idx, _ in enumerate(batch_labels):
            length = len(labels[idx])
            batch_labels[idx, :length] = np.array(labels[idx])

        return ms.Tensor(batch_labels, ms.int32)

    def _save_original_image(self, sample_id, image):
        """Save an image to summary directory."""
        id_dirname = self._get_sample_dirname(sample_id)
        path_tokens = [self._summary_dir,
                       self._DATAFILE_DIRNAME_PREFIX + str(self._summary_timestamp),
                       self._ORIGINAL_IMAGE_DIRNAME,
                       id_dirname]

        abs_dir_path = self._create_subdir(*path_tokens)
        filename = f"{sample_id}.jpg"
        save_path = os.path.join(abs_dir_path, filename)
        image.save(save_path)
        os.chmod(save_path, self._FILE_MODE)
        return os.path.join(*path_tokens[1:], filename)

    def _save_heatmap(self, explain_method, class_id, sample_id, image):
        """Save heatmap image to summary directory."""
        id_dirname = self._get_sample_dirname(sample_id)
        path_tokens = [self._summary_dir,
                       self._DATAFILE_DIRNAME_PREFIX + str(self._summary_timestamp),
                       self._HEATMAP_DIRNAME,
                       explain_method,
                       id_dirname]

        abs_dir_path = self._create_subdir(*path_tokens)
        filename = f"{sample_id}_{class_id}.jpg"
        save_path = os.path.join(abs_dir_path, filename)
        image.save(save_path, optimize=True)
        os.chmod(save_path, self._FILE_MODE)
        return os.path.join(*path_tokens[1:], filename)

    def _create_subdir(self, *args):
        """Recursively create subdirectories."""
        abs_path = None
        for token in args:
            if abs_path is None:
                abs_path = os.path.realpath(token)
            else:
                abs_path = os.path.join(abs_path, token)
            # os.makedirs() don't set intermediate dir permission properly, we mkdir() one by one
            try:
                os.mkdir(abs_path, mode=self._DIR_MODE)
                # In some platform, mode may be ignored in os.mkdir(), we have to chmod() again to make sure
                os.chmod(abs_path, mode=self._DIR_MODE)
            except FileExistsError:
                pass
        return abs_path

    @classmethod
    def _get_sample_dirname(cls, sample_id):
        """Get the name of parent directory of the image id."""
        return str(int(sample_id / cls._SAMPLE_PER_DIR) * cls._SAMPLE_PER_DIR)

    @staticmethod
    def _extract_timestamp(filename):
        """Extract timestamp from summary filename."""
        matched = re.search(r"summary\.(\d+)", filename)
        if matched:
            return int(matched.group(1))
        return None

    @classmethod
    def _spaced_print(cls, message, *args, **kwargs):
        """Spaced message printing."""
        # workaround to print logs starting new line in case line width mismatch.
        print(cls._SPACER.format(message))
