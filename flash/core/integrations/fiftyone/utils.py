from itertools import chain
from typing import List, Optional, Tuple, Union

import flash
from flash.core.data.data_module import DataModule
from flash.core.data.data_source import DefaultDataKeys
from flash.core.utilities.imports import _FIFTYONE_AVAILABLE

if _FIFTYONE_AVAILABLE:
    import fiftyone as fo
    from fiftyone.core.labels import Label
    from fiftyone.core.sample import Sample
    from fiftyone.core.session import Session
    from fiftyone.utils.data.parsers import LabeledImageTupleSampleParser
else:
    fo = None
    SampleCollection = None
    Label = None
    Sample = None
    Session = None


def fiftyone_visualize(
    labels: Union[List[Label], List[Tuple[str, Label]]],
    filepaths: Optional[List[str]] = None,
    datamodule: Optional[DataModule] = None,
    wait: Optional[bool] = True,
    label_field: Optional[str] = "predictions",
    **kwargs
) -> Optional[Session]:
    """Use the result of a FiftyOne serializer to visualize predictions in the
    FiftyOne App.

    Args:
        labels: Either a list of FiftyOne labels that will be applied to the
            corresponding filepaths provided with through `filepath` or
            `datamodule`. Or a list of tuples containing image/video
            filepaths and corresponding FiftyOne labels.
        filepaths: A list of filepaths to images or videos corresponding to the
            provided `labels`.
        datamodule: The datamodule containing the prediction dataset used to
            generate `labels`.
        wait: A boolean determining whether to launch the FiftyOne session and
            wait until the session is closed or whether to return immediately.
        label_field: The string of the label field in the FiftyOne dataset
            containing predictions
    """
    if not _FIFTYONE_AVAILABLE:
        raise ModuleNotFoundError("Please, `pip install fiftyone`.")
    if flash._IS_TESTING:
        return None

    # Flatten list if batches were used
    if all(isinstance(fl, list) for fl in labels):
        labels = list(chain.from_iterable(labels))

    if all(isinstance(fl, tuple) for fl in labels):
        filepaths = [lab[0] for lab in labels]
        labels = [lab[1] for lab in labels]

    if filepaths is None:
        if datamodule is None:
            raise ValueError("Either `filepaths` or `datamodule` arguments are "
                             "required if filepaths are not provided in `labels`.")

        else:
            filepaths = [s[DefaultDataKeys.FILEPATH] for s in datamodule.predict_dataset.data]

    dataset = fo.Dataset()
    if filepaths:
        dataset.add_labeled_images(
            list(zip(filepaths, labels)),
            LabeledImageTupleSampleParser(),
            label_field=label_field,
        )
    session = fo.launch_app(dataset, **kwargs)
    if wait:
        session.wait()
    return session


def get_classes(data, label_field: str):
    classes = data.classes.get(label_field, None)

    if not classes:
        classes = data.default_classes

    if not classes:
        label_path = data._get_label_field_path(label_field, "label")[1]
        classes = data.distinct(label_path)

    return classes
