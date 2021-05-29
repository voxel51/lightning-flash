# Copyright The PyTorch Lightning team.
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
import os
import typing
from dataclasses import dataclass
from inspect import signature
from typing import (
    Any,
    Callable,
    cast,
    Dict,
    Generic,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
    Union,
)

import numpy as np
import torch
from pytorch_lightning.trainer.states import RunningStage
from pytorch_lightning.utilities.enums import LightningEnum
from torch.nn import Module
from torch.utils.data.dataset import Dataset
from fiftyone.core.collections import SampleCollection

from flash.core.data.auto_dataset import AutoDataset, BaseAutoDataset, IterableAutoDataset
from flash.core.data.properties import ProcessState, Properties
from flash.core.data.utils import CurrentRunningStageFuncContext


# Credit to the PyTorchVision Team:
# https://github.com/pytorch/vision/blob/master/torchvision/datasets/folder.py#L10
def has_file_allowed_extension(filename: str, extensions: Tuple[str, ...]) -> bool:
    """Checks if a file is an allowed extension.

    Args:
        filename (string): path to a file
        extensions (tuple of strings): extensions to consider (lowercase)

    Returns:
        bool: True if the filename ends with one of given extensions
    """
    return filename.lower().endswith(extensions)


# Credit to the PyTorchVision Team:
# https://github.com/pytorch/vision/blob/master/torchvision/datasets/folder.py#L48
def make_dataset(
    directory: str,
    class_to_idx: Dict[str, int],
    extensions: Optional[Tuple[str, ...]] = None,
    is_valid_file: Optional[Callable[[str], bool]] = None,
) -> List[Tuple[str, int]]:
    """Generates a list of samples of a form (path_to_sample, class).

    Args:
        directory (str): root dataset directory
        class_to_idx (Dict[str, int]): dictionary mapping class name to class index
        extensions (optional): A list of allowed extensions.
            Either extensions or is_valid_file should be passed. Defaults to None.
        is_valid_file (optional): A function that takes path of a file
            and checks if the file is a valid file
            (used to check of corrupt files) both extensions and
            is_valid_file should not be passed. Defaults to None.

    Raises:
        ValueError: In case ``extensions`` and ``is_valid_file`` are None or both are not None.

    Returns:
        List[Tuple[str, int]]: samples of a form (path_to_sample, class)
    """
    instances = []
    directory = os.path.expanduser(directory)
    both_none = extensions is None and is_valid_file is None
    both_something = extensions is not None and is_valid_file is not None
    if both_none or both_something:
        raise ValueError("Both extensions and is_valid_file cannot be None or not None at the same time")
    if extensions is not None:

        def is_valid_file(x: str) -> bool:
            return has_file_allowed_extension(x, cast(Tuple[str, ...], extensions))

    is_valid_file = cast(Callable[[str], bool], is_valid_file)
    for target_class in sorted(class_to_idx.keys()):
        class_index = class_to_idx[target_class]
        target_dir = os.path.join(directory, target_class)
        if not os.path.isdir(target_dir):
            continue
        for root, _, fnames in sorted(os.walk(target_dir, followlinks=True)):
            for fname in sorted(fnames):
                path = os.path.join(root, fname)
                if is_valid_file(path):
                    item = path, class_index
                    instances.append(item)
    return instances


def has_len(data: Union[Sequence[Any], Iterable[Any]]) -> bool:
    try:
        len(data)
        return True
    except (TypeError, NotImplementedError):
        return False


@dataclass(unsafe_hash=True, frozen=True)
class LabelsState(ProcessState):
    """
    A :class:`~flash.core.data.properties.ProcessState` containing ``labels``,
    a mapping from class index to label.
    """

    labels: Optional[Sequence[str]]


@dataclass(unsafe_hash=True, frozen=True)
class ImageLabelsMap(ProcessState):

    labels_map: Optional[Dict[int, Tuple[int, int, int]]]


class DefaultDataSources(LightningEnum):
    """The ``DefaultDataSources`` enum contains the data source names used by all of the default ``from_*`` methods in
    :class:`~flash.core.data.data_module.DataModule`."""

    FOLDERS = "folders"
    FILES = "files"
    NUMPY = "numpy"
    TENSORS = "tensors"
    CSV = "csv"
    JSON = "json"
    DATASET = "dataset"
    FIFTYONE = "fiftyone"

    # TODO: Create a FlashEnum class???
    def __hash__(self) -> int:
        return hash(self.value)


class DefaultDataKeys(LightningEnum):
    """The ``DefaultDataKeys`` enum contains the keys that are used by built-in data sources to refer to inputs and
    targets."""

    INPUT = "input"
    PREDS = "preds"
    TARGET = "target"
    METADATA = "metadata"

    # TODO: Create a FlashEnum class???
    def __hash__(self) -> int:
        return hash(self.value)


class MockDataset:
    """The ``MockDataset`` catches any metadata that is attached through ``__setattr__``. This is passed to
    :meth:`~flash.core.data.data_source.DataSource.load_data` so that attributes can be set on the generated
    data set."""

    def __init__(self):
        self.metadata = {}

    def __setattr__(self, key, value):
        if key != 'metadata':
            self.metadata[key] = value
        object.__setattr__(self, key, value)


DATA_TYPE = TypeVar("DATA_TYPE")


class DataSource(Generic[DATA_TYPE], Properties, Module):
    """The ``DataSource`` class encapsulates two hooks: ``load_data`` and ``load_sample``. The
    :meth:`~flash.core.data.data_source.DataSource.to_datasets` method can then be used to automatically construct data
    sets from the hooks."""

    def load_data(self,
                  data: DATA_TYPE,
                  dataset: Optional[Any] = None) -> Union[Sequence[Mapping[str, Any]], Iterable[Mapping[str, Any]]]:
        """Given the ``data`` argument, the ``load_data`` hook produces a sequence or iterable of samples or
        sample metadata. The ``data`` argument can be anything, but this method should return a sequence or iterable of
        mappings from string (e.g. "input", "target", "bbox", etc.) to data (e.g. a target value) or metadata (e.g. a
        filename). Where possible, any heavy data loading should be performed in
        :meth:`~flash.core.data.data_source.DataSource.load_sample`. If the output is an iterable rather than a sequence
        (that is, it doesn't have length) then the generated dataset will be an ``IterableDataset``.

        Args:
            data: The data required to load the sequence or iterable of samples or sample metadata.
            dataset: Overriding methods can optionally include the dataset argument. Any attributes set on the dataset
                (e.g. ``num_classes``) will also be set on the generated dataset.

        Returns:
            A sequence or iterable of samples or sample metadata to be used as inputs to
            :meth:`~flash.core.data.data_source.DataSource.load_sample`.

        Example::

            # data: "."
            # output: [{"input": "./cat/1.png", "target": 1}, ..., {"input": "./dog/10.png", "target": 0}]

            output: Sequence[Mapping[str, Any]] = load_data(data)

        """
        return data

    def load_sample(self, sample: Mapping[str, Any], dataset: Optional[Any] = None) -> Any:
        """Given an element from the output of a call to :meth:`~flash.core.data.data_source.DataSource.load_data`, this hook
        should load a single data sample. The keys and values in the ``sample`` argument will be same as the keys and
        values in the outputs of :meth:`~flash.core.data.data_source.DataSource.load_data`.

        Args:
            sample: An element (sample or sample metadata) from the output of a call to
                :meth:`~flash.core.data.data_source.DataSource.load_data`.
            dataset: Overriding methods can optionally include the dataset argument. Any attributes set on the dataset
                (e.g. ``num_classes``) will also be set on the generated dataset.

        Returns:
            The loaded sample as a mapping with string keys (e.g. "input", "target") that can be processed by the
            :meth:`~flash.core.data.process.Preprocess.pre_tensor_transform`.

        Example::

            # sample: {"input": "./cat/1.png", "target": 1}
            # output: {"input": PIL.Image, "target": 1}

            output: Mapping[str, Any] = load_sample(sample)

        """
        return sample

    def to_datasets(
        self,
        train_data: Optional[DATA_TYPE] = None,
        val_data: Optional[DATA_TYPE] = None,
        test_data: Optional[DATA_TYPE] = None,
        predict_data: Optional[DATA_TYPE] = None,
    ) -> Tuple[Optional[BaseAutoDataset], ...]:
        """Construct data sets (of type :class:`~flash.core.data.auto_dataset.BaseAutoDataset`) from this data source by
        calling :meth:`~flash.core.data.data_source.DataSource.load_data` with each of the ``*_data`` arguments. If an
        argument is given as ``None`` then no dataset will be created for that stage (``train``, ``val``, ``test``,
        ``predict``).

        Args:
            train_data: The input to :meth:`~flash.core.data.data_source.DataSource.load_data` to use to create the
                train dataset.
            val_data: The input to :meth:`~flash.core.data.data_source.DataSource.load_data` to use to create the
                validation dataset.
            test_data: The input to :meth:`~flash.core.data.data_source.DataSource.load_data` to use to create the
                test dataset.
            predict_data: The input to :meth:`~flash.core.data.data_source.DataSource.load_data` to use to create
                the predict dataset.

        Returns:
            A tuple of ``train_dataset``, ``val_dataset``, ``test_dataset``, ``predict_dataset``. If any ``*_data``
            argument is not passed to this method then the corresponding ``*_dataset`` will be ``None``.
        """
        train_dataset = self.generate_dataset(train_data, RunningStage.TRAINING)
        val_dataset = self.generate_dataset(val_data, RunningStage.VALIDATING)
        test_dataset = self.generate_dataset(test_data, RunningStage.TESTING)
        predict_dataset = self.generate_dataset(predict_data, RunningStage.PREDICTING)
        return train_dataset, val_dataset, test_dataset, predict_dataset

    def generate_dataset(
        self,
        data: Optional[DATA_TYPE],
        running_stage: RunningStage,
    ) -> Optional[Union[AutoDataset, IterableAutoDataset]]:
        """Generate a single dataset with the given input to :meth:`~flash.core.data.data_source.DataSource.load_data` for
        the given ``running_stage``.

        Args:
            data: The input to :meth:`~flash.core.data.data_source.DataSource.load_data` to use to create the dataset.
            running_stage: The running_stage for this dataset.

        Returns:
            The constructed :class:`~flash.core.data.auto_dataset.BaseAutoDataset`.
        """
        is_none = data is None

        if isinstance(data, Sequence):
            is_none = data[0] is None

        if not is_none:
            from flash.core.data.data_pipeline import DataPipeline

            mock_dataset = typing.cast(AutoDataset, MockDataset())
            with CurrentRunningStageFuncContext(running_stage, "load_data", self):
                resolved_func_name = DataPipeline._resolve_function_hierarchy(
                    "load_data", self, running_stage, DataSource
                )
                load_data: Callable[[DATA_TYPE, Optional[Any]], Any] = getattr(self, resolved_func_name)
                parameters = signature(load_data).parameters
                if len(parameters) > 1 and "dataset" in parameters:  # TODO: This was DATASET_KEY before
                    data = load_data(data, mock_dataset)
                else:
                    data = load_data(data)

            if has_len(data):
                dataset = AutoDataset(data, self, running_stage)
            else:
                dataset = IterableAutoDataset(data, self, running_stage)
            dataset.__dict__.update(mock_dataset.metadata)
            return dataset


SEQUENCE_DATA_TYPE = TypeVar("SEQUENCE_DATA_TYPE")


class DatasetDataSource(DataSource):

    def load_data(self, dataset: Dataset, auto_dataset: AutoDataset) -> Dataset:
        if self.training:
            # store a sample to infer the shape
            parameters = signature(self.load_sample).parameters
            if len(parameters) > 1 and AutoDataset.DATASET_KEY in parameters:
                auto_dataset.sample = self.load_sample(dataset[0], self)
            else:
                auto_dataset.sample = self.load_sample(dataset[0])
        return dataset

    def load_sample(self, sample: Mapping[str, Any], dataset: Optional[Any]) -> Any:
        # wrap everything within `.INPUT`.
        return {DefaultDataKeys.INPUT: sample}


class SequenceDataSource(
    Generic[SEQUENCE_DATA_TYPE],
    DataSource[Tuple[Sequence[SEQUENCE_DATA_TYPE], Optional[Sequence]]],
):
    """The ``SequenceDataSource`` implements default behaviours for data sources which expect the input to
    :meth:`~flash.core.data.data_source.DataSource.load_data` to be a sequence of tuples (``(input, target)``
        where target can be ``None``).

    Args:
        labels: Optionally pass the labels as a mapping from class index to label string. These will then be set as the
        :class:`~flash.core.data.data_source.LabelsState`.
    """

    def __init__(self, labels: Optional[Sequence[str]] = None):
        super().__init__()

        self.labels = labels

        if self.labels is not None:
            self.set_state(LabelsState(self.labels))

    def load_data(
        self,
        data: Tuple[Sequence[SEQUENCE_DATA_TYPE], Optional[Sequence]],
        dataset: Optional[Any] = None,
    ) -> Sequence[Mapping[str, Any]]:
        # TODO: Bring back the code to work out how many classes there are
        inputs, targets = data
        if targets is None:
            return self.predict_load_data(data)
        return [{
            DefaultDataKeys.INPUT: input,
            DefaultDataKeys.TARGET: target
        } for input, target in zip(inputs, targets)]

    def predict_load_data(self, data: Sequence[SEQUENCE_DATA_TYPE]) -> Sequence[Mapping[str, Any]]:
        return [{DefaultDataKeys.INPUT: input} for input in data]


class PathsDataSource(SequenceDataSource):
    """The ``PathsDataSource`` implements default behaviours for data sources which expect the input to
    :meth:`~flash.core.data.data_source.DataSource.load_data` to be either a directory with a subdirectory for
    each class or a tuple containing list of files and corresponding list of targets.

    Args:
        extensions: The file extensions supported by this data source (e.g. ``(".jpg", ".png")``).
        labels: Optionally pass the labels as a mapping from class index to label string. These will then be set as the
        :class:`~flash.core.data.data_source.LabelsState`.
    """

    def __init__(self, extensions: Optional[Tuple[str, ...]] = None, labels: Optional[Sequence[str]] = None):
        super().__init__(labels=labels)

        self.extensions = extensions

    @staticmethod
    def find_classes(dir: str) -> Tuple[List[str], Dict[str, int]]:
        """Finds the class folders in a dataset. Ensures that no class is a subdirectory of another.

        Args:
            dir: Root directory path.

        Returns:
            tuple: (classes, class_to_idx) where classes are relative to (dir), and class_to_idx is a dictionary.
        """
        classes = [d.name for d in os.scandir(dir) if d.is_dir()]
        classes.sort()
        class_to_idx = {cls_name: i for i, cls_name in enumerate(classes)}
        return classes, class_to_idx

    @staticmethod
    def isdir(data: Union[str, Tuple[List[str], List[Any]]]) -> bool:
        try:
            return os.path.isdir(data)
        except TypeError:
            # data is not path-like (e.g. it may be a list of paths)
            return False

    def load_data(self,
                  data: Union[str, Tuple[List[str], List[Any]]],
                  dataset: Optional[Any] = None) -> Sequence[Mapping[str, Any]]:
        if self.isdir(data):
            classes, class_to_idx = self.find_classes(data)
            if not classes:
                return self.predict_load_data(data)
            else:
                self.set_state(LabelsState(classes))

            if dataset is not None:
                dataset.num_classes = len(classes)

            data = make_dataset(data, class_to_idx, extensions=self.extensions)
            return [{DefaultDataKeys.INPUT: input, DefaultDataKeys.TARGET: target} for input, target in data]
        return list(
            filter(
                lambda sample: has_file_allowed_extension(sample[DefaultDataKeys.INPUT], self.extensions),
                super().load_data(data, dataset),
            )
        )

    def predict_load_data(self,
                          data: Union[str, List[str]],
                          dataset: Optional[Any] = None) -> Sequence[Mapping[str, Any]]:
        if self.isdir(data):
            data = [os.path.join(data, file) for file in os.listdir(data)]

        if not isinstance(data, list):
            data = [data]

        return list(
            filter(
                lambda sample: has_file_allowed_extension(sample[DefaultDataKeys.INPUT], self.extensions),
                super().predict_load_data(data),
            )
        )


class TensorDataSource(SequenceDataSource[torch.Tensor]):
    """The ``TensorDataSource`` is a ``SequenceDataSource`` which expects the input to
    :meth:`~flash.core.data.data_source.DataSource.load_data` to be a sequence of ``torch.Tensor`` objects."""


class NumpyDataSource(SequenceDataSource[np.ndarray]):
    """The ``NumpyDataSource`` is a ``SequenceDataSource`` which expects the input to
    :meth:`~flash.core.data.data_source.DataSource.load_data` to be a sequence of ``np.ndarray`` objects."""


class FiftyOneDataSource(SequenceDataSource[SampleCollection]):
    """The ``FiftyOneDataSource`` is a ``SequenceDataSource`` which expects the input to
    :meth:`~flash.core.data.data_source.DataSource.load_data` to be a sequence
    of FiftyOne Dataset objects."""

    def predict_load_data(self,
                          data: SampleCollection,
                          dataset: Optional[Any] = None) -> Sequence[Mapping[str, Any]]:
        return [{DefaultDataKeys.INPUT: s.filepath} for s in data] 
