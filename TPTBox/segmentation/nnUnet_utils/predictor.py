# Adapted from https://github.com/MIC-DKFZ/nnUNet
# Isensee, F., Jaeger, P. F., Kohl, S. A., Petersen, J., & Maier-Hein, K. H. (2021). nnU-Net: a self-configuring
# method for deep learning-based biomedical image segmentation. Nature methods, 18(2), 203-211.
from __future__ import annotations

import os
import traceback
from pathlib import Path

import numpy as np
import torch
from acvl_utils.cropping_and_padding.padding import pad_nd_image
from batchgenerators.utilities.file_and_folder_operations import join, load_json, load_pickle
from nnunetv2.utilities.label_handling.label_handling import determine_num_input_channels
from torch._dynamo import OptimizedModule
from tqdm import tqdm

from TPTBox.core.compat import zip_strict
from TPTBox.segmentation.nnUnet_utils.data_iterators import PreprocessAdapterFromNpy
from TPTBox.segmentation.nnUnet_utils.export_prediction import (
    convert_predicted_logits_to_segmentation_with_correct_shape,
    export_prediction_from_logits,
)
from TPTBox.segmentation.nnUnet_utils.get_network_from_plans import get_network_from_plans
from TPTBox.segmentation.nnUnet_utils.plans_handler import PlansManager
from TPTBox.segmentation.nnUnet_utils.sliding_window_prediction import compute_gaussian, compute_steps_for_sliding_window


class nnUNetPredictor:
    def __init__(
        self,
        tile_step_size: float = 0.5,
        use_gaussian: bool = True,
        use_mirroring: bool = True,
        perform_everything_on_gpu: bool = True,
        device: torch.device = torch.device("cuda"),  # noqa: B008
        cuda_id=0,
        verbose: bool = False,
        verbose_preprocessing: bool = False,
        allow_tqdm: bool = True,
    ):
        self.verbose = verbose
        self.verbose_preprocessing = verbose_preprocessing
        self.allow_tqdm = allow_tqdm

        (
            self.plans_manager,
            self.configuration_manager,
            self.list_of_parameters,
            self.network,
            self.dataset_json,
            self.trainer_name,
            self.allowed_mirroring_axes,
            self.label_manager,
        ) = (None, None, None, None, None, None, None, None)

        self.tile_step_size = tile_step_size
        self.use_gaussian = use_gaussian
        self.use_mirroring = use_mirroring
        if device.type == "cuda":
            device = torch.device(type="cuda", index=cuda_id)  # set the desired GPU with CUDA_VISIBLE_DEVICES!
        if device.type != "cuda" and perform_everything_on_gpu:
            print("perform_everything_on_gpu=True is only supported for cuda devices! Setting this to False")
            perform_everything_on_gpu = False
        self.device = device
        self.perform_everything_on_gpu = perform_everything_on_gpu

    def initialize_from_trained_model_folder(
        self,
        model_training_output_dir: str,
        use_folds: tuple[int | str, ...] | None,
        checkpoint_name: str = "checkpoint_final.pth",
        cache_state_dicts: bool = True,
    ):
        """
        This is used when making predictions with a trained model
        """
        if isinstance(use_folds, str):
            use_folds = [use_folds]  # type: ignore
        if use_folds is None:
            use_folds = ("0", "1", "2", "3", "4")
        pkl_file = join(model_training_output_dir, "plans.pkl")
        if os.path.exists(pkl_file):  # noqa: PTH110
            ## LOAD NNUNET 1 models
            from nnunet.training.model_restore import restore_model

            pkl_file1 = join(model_training_output_dir, f"fold_{use_folds[0]}", "model_final_checkpoint.model.pkl")  # type: ignore
            trainer = restore_model(pkl_file1, fp16=True)
            trainer.output_folder = model_training_output_dir
            trainer.output_folder_base = model_training_output_dir
            trainer.update_fold(0)
            trainer.initialize(False)
            all_best_model_files = [join(model_training_output_dir, f"fold_{i}", "model_final_checkpoint.model") for i in use_folds]
            print("using the following model files: ", all_best_model_files)
            all_params = [torch.load(i, map_location=torch.device("cpu"))["state_dict"] for i in all_best_model_files]
            plans = trainer.plans
            assert plans is not None
            plans["plans_per_stage"][0]["spacing"] = plans["plans_per_stage"][0]["original_spacing"]
            plans["configurations"] = {
                "plans_manager_": {
                    "spacing": plans["plans_per_stage"][0]["original_spacing"],
                    "normalization_schemes": [
                        "CTNormalization" if i == "CT" else "ZScoreNormalization" for i in plans["normalization_schemes"].values()
                    ],
                    "use_mask_for_norm": plans["use_mask_for_norm"],
                    "resampling_fn_data": "resample_data_or_seg_to_shape",
                    "resampling_fn_data_kwargs": {"is_seg": False, "order": 3, "order_z": 0, "force_separate_z": None},
                    "resampling_fn_seg": "resample_data_or_seg_to_shape",
                    "resampling_fn_seg_kwargs": {"is_seg": True, "order": 1, "order_z": 0, "force_separate_z": None},
                    "resampling_fn_probabilities": "resample_data_or_seg_to_shape",
                    "resampling_fn_probabilities_kwargs": {"is_seg": False, "order": 1, "order_z": 0, "force_separate_z": None},
                    **plans["plans_per_stage"][0],
                }
            }

            def mapp(d: dict):
                d["std"] = d["sd"]
                d["min"] = d["mn"]
                d["max"] = d["mx"]
                return d

            print(plans.keys())
            plans["foreground_intensity_properties_per_channel"] = {
                str(i): mapp(k) for i, k in plans["dataset_properties"]["intensityproperties"].items()
            }
            plans_manager = PlansManager(plans)
            self.plans_manager = plans_manager
            self.dataset_json = dataset_json = {}
            if hasattr(trainer, "regions_class_order"):
                self.dataset_json["regions_class_order"] = trainer.regions_class_order
            self.dataset_json["labels"] = {str(i): i for i in trainer.classes}  # type: ignore
            self.dataset_json["channel_names"] = list(self.dataset_json["labels"].values())
            self.dataset_json["labels"]["background"] = 0

            configuration_manager = plans_manager.get_configuration("plans_manager_")
            self.configuration_manager = configuration_manager
            self.list_of_parameters = all_params
            num_input_channels = determine_num_input_channels(plans_manager, configuration_manager, self.dataset_json)  # type: ignore
            self.network = trainer.network

        else:
            dataset_json = load_json(join(model_training_output_dir, "dataset.json"))
            plans = load_json(join(model_training_output_dir, "plans.json"))

            parameters = []
            for i, f in enumerate(use_folds):
                f = int(f) if f != "all" else f  # noqa: PLW2901
                checkpoint = torch.load(
                    join(model_training_output_dir, f"fold_{f}", checkpoint_name), map_location=torch.device("cpu"), weights_only=False
                )
                if i == 0:
                    trainer_name = checkpoint["trainer_name"]
                    configuration_name = checkpoint["init_args"]["configuration"]
                    inference_allowed_mirroring_axes = (
                        checkpoint["inference_allowed_mirroring_axes"] if "inference_allowed_mirroring_axes" in checkpoint.keys() else None
                    )

                parameters.append(checkpoint["network_weights"])
                plans_manager = PlansManager(plans)
                configuration_manager = plans_manager.get_configuration(configuration_name)

            # restore network
            num_input_channels = determine_num_input_channels(plans_manager, configuration_manager, dataset_json)  # type: ignore
            # num_input_channels = 1
        num_output_channels = len(dataset_json["labels"])
        if "ignore" in dataset_json["labels"]:
            num_output_channels -= 1

        network = get_network_from_plans(
            plans_manager,  # type: ignore
            dataset_json,
            configuration_manager,  # type: ignore
            num_input_channels,
            num_output_channels=num_output_channels,
            deep_supervision=False,
        )
        self.network = network

        self.plans_manager = plans_manager
        self.configuration_manager = configuration_manager
        self.list_of_parameters = parameters  # Lists of model folds
        self.dataset_json = dataset_json
        self.trainer_name = trainer_name
        self.allowed_mirroring_axes = inference_allowed_mirroring_axes
        self.label_manager = plans_manager.get_label_manager(dataset_json)
        if (
            ("nnUNet_compile" in os.environ.keys())
            and (os.environ["nnUNet_compile"].lower() in ("true", "1", "t"))  # noqa: SIM112
            and not isinstance(self.network, OptimizedModule)
        ):
            print("compiling network")
            self.network = torch.compile(self.network)  # type: ignore

        self.loaded_networks = []
        if cache_state_dicts:
            for params in self.list_of_parameters:
                if not isinstance(self.network, OptimizedModule):
                    self.network.load_state_dict(params)  # type: ignore
                else:
                    self.network._orig_mod.load_state_dict(params)
                self.network.cuda()  # type: ignore
                self.network.eval()  # type: ignore
                self.loaded_networks.append(self.network)
        # print(type(self.loaded_networks[0]))

    def predict_single_npy_array(
        self,
        input_image: np.ndarray,
        image_properties: dict,
        save_or_return_probabilities: bool = False,
    ):
        """
        image_properties must only have a 'spacing' key!
        """
        segmentation_previous_stage: np.ndarray = None  # type: ignore # Was previously a parameter
        output_file_truncated: str = None  # type: ignore # previously a parameter

        ppa = PreprocessAdapterFromNpy(
            [input_image],
            [segmentation_previous_stage],
            [image_properties],
            [output_file_truncated],
            self.plans_manager,  # type: ignore
            self.dataset_json,  # type: ignore
            self.configuration_manager,  # type: ignore
            num_threads_in_multithreaded=1,
            verbose=self.verbose,
        )
        if self.verbose:
            print("preprocessing")
        dct = next(ppa)

        if self.verbose:
            print("predicting")
        (
            predicted_logits,
            prediction_stacked,
        ) = self.predict_logits_from_preprocessed_data(dct["data"])  # type: ignore
        predicted_logits.cpu()
        prediction_stacked.cpu()

        if self.verbose:
            print("resampling to original shape")
        if output_file_truncated is not None:
            print("export_prediction_from_logits")
            export_prediction_from_logits(
                predicted_logits,
                dct["data_properites"],  # type: ignore
                self.configuration_manager,  # type: ignore
                self.plans_manager,  # type: ignore
                self.dataset_json,  # type: ignore
                output_file_truncated,
                save_or_return_probabilities,
            )
        else:
            print("convert_predicted_logits_to_segmentation_with_correct_shape", predicted_logits.shape)
            ret = convert_predicted_logits_to_segmentation_with_correct_shape(
                predicted_logits,
                self.plans_manager,
                self.configuration_manager,
                self.label_manager,
                dct["data_properites"],
                return_probabilities=save_or_return_probabilities,
            )
            if prediction_stacked is not None:
                seg_stacked = None
                # print("convert_predicted_logits_to_segmentation_with_correct_shape - v2")
                # seg_stacked = np.stack(
                #    list(
                #        [
                #            convert_predicted_logits_to_segmentation_with_correct_shape(
                #                r,
                #                self.plans_manager,
                #                self.configuration_manager,
                #                self.label_manager,
                #                dct["data_properites"],
                #                return_probabilities=False,
                #            )
                #            for r in prediction_stacked
                #        ]
                #    )
                # )

            if save_or_return_probabilities:
                return ret[0], seg_stacked, ret[1]
            else:
                return ret, seg_stacked

    def predict_logits_from_preprocessed_data(self, data: torch.Tensor) -> torch.Tensor:
        """
        IMPORTANT! IF YOU ARE RUNNING THE CASCADE, THE SEGMENTATION FROM THE PREVIOUS STAGE MUST ALREADY BE STACKED ON
        TOP OF THE IMAGE AS ONE-HOT REPRESENTATION! SEE PreprocessAdapter ON HOW THIS SHOULD BE DONE!

        RETURNED LOGITS HAVE THE SHAPE OF THE INPUT. THEY MUST BE CONVERTED BACK TO THE ORIGINAL IMAGE SIZE.
        SEE convert_predicted_logits_to_segmentation_with_correct_shape
        """
        # USED

        # we have some code duplication here but this allows us to run with perform_everything_on_gpu=True as
        # default and not have the entire program crash in case of GPU out of memory. Neat. That should make
        # things a lot faster for some datasets.
        original_perform_everything_on_gpu = self.perform_everything_on_gpu
        assert self.list_of_parameters is not None
        with torch.no_grad():
            prediction = None
            prediction_stacked = None
            if self.perform_everything_on_gpu:
                try:
                    for idx, params in enumerate(self.list_of_parameters):
                        network = None
                        if self.loaded_networks is not None:
                            network = self.loaded_networks[idx]
                        # messing with state dict names...
                        elif not isinstance(self.network, OptimizedModule):
                            self.network.load_state_dict(params)  # type: ignore
                        else:
                            self.network._orig_mod.load_state_dict(params)
                        # print(type(self.network))

                        if prediction is None:
                            prediction = self.predict_sliding_window_return_logits(data, network=network).to("cpu")  # type: ignore
                            prediction_stacked = [prediction]
                        else:
                            new_prediction = self.predict_sliding_window_return_logits(data, network=network).to("cpu")  # type: ignore
                            prediction += new_prediction
                            prediction_stacked.append(new_prediction)  # type: ignore

                    if len(self.list_of_parameters) > 1:
                        prediction /= len(self.list_of_parameters)  # type: ignore
                    # empty_cache(self.device)

                except RuntimeError:
                    print(
                        "Prediction with perform_everything_on_gpu=True failed due to insufficient GPU memory. "
                        "Falling back to perform_everything_on_gpu=False. Not a big deal, just slower..."
                    )
                    print("Error:")
                    traceback.print_exc()
                    prediction = None
                    self.perform_everything_on_gpu = False

            # CPU version
            if prediction is None:
                print("FALL BACK CPU")
                for idx, params in enumerate(self.list_of_parameters):
                    network = None
                    if self.loaded_networks is not None:
                        network = self.loaded_networks[idx]
                    # messing with state dict names...
                    elif not isinstance(self.network, OptimizedModule):
                        self.network.load_state_dict(params)  # type: ignore
                    else:
                        self.network._orig_mod.load_state_dict(params)

                    if prediction is None:
                        prediction = self.predict_sliding_window_return_logits(data, network=network).to("cpu")  # type: ignore
                        prediction_stacked = [prediction]
                    else:
                        new_prediction = self.predict_sliding_window_return_logits(data, network=network).to("cpu")  # type: ignore
                        prediction += new_prediction
                        prediction_stacked.append(new_prediction)  # type: ignore

                if len(self.list_of_parameters) > 1:
                    prediction /= len(self.list_of_parameters)  # type: ignore
            del data
            print("Prediction done, transferring to CPU if needed")  # if self.verbose else None
            prediction = prediction.to("cpu")  # type: ignore
            self.perform_everything_on_gpu = original_perform_everything_on_gpu
        print("Stack predictions", len(prediction_stacked), prediction_stacked[-1].shape)  # type: ignore # type: ignore
        s = torch.stack(prediction_stacked)  # type: ignore
        print("logit_shape", s.shape)
        return prediction, s  # type: ignore

    def _internal_get_sliding_window_slicers(self, image_size: tuple[int, ...]):
        # USED
        slicers = []
        assert self.configuration_manager is not None
        if len(self.configuration_manager.patch_size) < len(image_size):
            assert len(self.configuration_manager.patch_size) == len(image_size) - 1, (
                "if tile_size has less entries than image_size, "
                "len(tile_size) "
                "must be one shorter than len(image_size) "
                "(only dimension "
                "discrepancy of 1 allowed)."
            )
            steps = compute_steps_for_sliding_window(
                image_size[1:],
                self.configuration_manager.patch_size,  # type: ignore
                self.tile_step_size,
            )
            if self.verbose:
                print(
                    f"n_steps {image_size[0] * len(steps[0]) * len(steps[1])}, image size is"
                    f" {image_size}, tile_size {self.configuration_manager.patch_size}, "
                    f"tile_step_size {self.tile_step_size}\nsteps:\n{steps}"
                )
            for d in range(image_size[0]):
                for sx in steps[0]:
                    for sy in steps[1]:
                        x__ = [slice(si, si + ti) for si, ti in zip_strict((sx, sy), self.configuration_manager.patch_size)]
                        slicers.append((slice(None), d, *x__))
        else:
            steps = compute_steps_for_sliding_window(image_size, self.configuration_manager.patch_size, self.tile_step_size)  # type: ignore
            if self.verbose:
                print(
                    f"n_steps {np.prod([len(i) for i in steps])}, image size is {image_size}, tile_size {self.configuration_manager.patch_size}, "
                    f"tile_step_size {self.tile_step_size}\nsteps:\n{steps}"
                )
            for sx in steps[0]:
                for sy in steps[1]:
                    for sz in steps[2]:
                        x__ = [slice(si, si + ti) for si, ti in zip_strict((sx, sy, sz), self.configuration_manager.patch_size)]
                        slicers.append((slice(None), *x__))
        return slicers

    def _internal_maybe_mirror_and_predict(self, x: torch.Tensor, network) -> torch.Tensor:
        # USED
        mirror_axes = self.allowed_mirroring_axes if self.use_mirroring else None
        prediction = network(x)

        if mirror_axes is not None:
            # check for invalid numbers in mirror_axes
            # x should be 5d for 3d images and 4d for 2d. so the max value of mirror_axes cannot exceed len(x.shape) - 3
            assert max(mirror_axes) <= len(x.shape) - 3, "mirror_axes does not match the dimension of the input!"

            num_predictons = 2 ** len(mirror_axes)
            if 0 in mirror_axes:
                prediction += torch.flip(network(torch.flip(x, (2,))), (2,))
            if 1 in mirror_axes:
                prediction += torch.flip(network(torch.flip(x, (3,))), (3,))
            if 2 in mirror_axes:
                prediction += torch.flip(network(torch.flip(x, (4,))), (4,))
            if 0 in mirror_axes and 1 in mirror_axes:
                prediction += torch.flip(network(torch.flip(x, (2, 3))), (2, 3))
            if 0 in mirror_axes and 2 in mirror_axes:
                prediction += torch.flip(network(torch.flip(x, (2, 4))), (2, 4))
            if 1 in mirror_axes and 2 in mirror_axes:
                prediction += torch.flip(network(torch.flip(x, (3, 4))), (3, 4))
            if 0 in mirror_axes and 1 in mirror_axes and 2 in mirror_axes:
                prediction += torch.flip(network(torch.flip(x, (2, 3, 4))), (2, 3, 4))
            prediction /= num_predictons
        return prediction

    def predict_sliding_window_return_logits(self, input_image: torch.Tensor, network=None) -> np.ndarray | torch.Tensor:
        # USED
        assert isinstance(input_image, torch.Tensor)
        if network is None:
            network = self.network
            network.eval()  # type: ignore
        network = network.to(self.device)  # type: ignore
        assert self.configuration_manager is not None
        assert self.label_manager is not None
        # empty_cache(self.device)

        # Autocast is a little bitch.
        # If the device_type is 'cpu' then it's slow as heck on some CPUs (no auto bfloat16 support detection)
        # and needs to be disabled.
        # If the device_type is 'mps' then it will complain that mps is not implemented, even if enabled=False
        # is set. Why. (this is why we don't make use of enabled=False)
        # So autocast will only be active if we have a cuda device.
        with torch.no_grad(), torch.autocast(self.device.type, enabled=True) if self.device.type == "cuda" else dummy_context():
            assert len(input_image.shape) == 4, "input_image must be a 4D np.ndarray or torch.Tensor (c, x, y, z)"

            if self.verbose:
                print(f"Input shape: {input_image.shape}")
            if self.verbose:
                print("step_size:", self.tile_step_size)
            if self.verbose:
                print(
                    "mirror_axes:",
                    self.allowed_mirroring_axes if self.use_mirroring else None,
                )

            # if input_image is smaller than tile_size we need to pad it to tile_size.
            data, slicer_revert_padding = pad_nd_image(
                input_image,
                self.configuration_manager.patch_size,  # type: ignore
                "constant",
                {"value": 0},
                True,
                None,  # type: ignore
            )

            slicers = self._internal_get_sliding_window_slicers(data.shape[1:])

            # preallocate results and num_predictions
            results_device = self.device if self.perform_everything_on_gpu else torch.device("cpu")
            if self.verbose:
                print("preallocating arrays")
            try:
                data = data.to(self.device)  # type: ignore
                predicted_logits = torch.zeros(
                    (self.label_manager.num_segmentation_heads, *data.shape[1:]),  # type: ignore
                    dtype=torch.half,
                    device=results_device,
                )
                n_predictions = torch.zeros(data.shape[1:], dtype=torch.half, device=results_device)
                if self.use_gaussian:
                    gaussian = compute_gaussian(
                        tuple(self.configuration_manager.patch_size),
                        sigma_scale=1.0 / 8,
                        value_scaling_factor=1000,
                        device=results_device,
                    )
            except RuntimeError:
                # sometimes the stuff is too large for GPUs. In that case fall back to CPU
                results_device = torch.device("cpu")
                data = data.to(results_device)  # type: ignore
                predicted_logits = torch.zeros(
                    (self.label_manager.num_segmentation_heads, *data.shape[1:]),
                    dtype=torch.half,
                    device=results_device,
                )
                n_predictions = torch.zeros(data.shape[1:], dtype=torch.half, device=results_device)
                if self.use_gaussian:
                    gaussian = compute_gaussian(
                        tuple(self.configuration_manager.patch_size),
                        sigma_scale=1.0 / 8,
                        value_scaling_factor=1000,
                        device=results_device,
                    )
            finally:
                empty_cache(self.device)

            if self.verbose:
                print("running prediction")
            for sl in tqdm(slicers, disable=not self.allow_tqdm):
                work_on = data[sl][None]
                work_on = work_on.to(self.device, non_blocking=False)

                prediction = self._internal_maybe_mirror_and_predict(work_on, network=network)[0].to(results_device)
                if prediction.shape[0] != predicted_logits.shape[0]:
                    prediction.squeeze_(0)
                predicted_logits[sl] += prediction * gaussian if self.use_gaussian else prediction
                n_predictions[sl[1:]] += gaussian if self.use_gaussian else 1

            predicted_logits /= n_predictions
        # empty_cache(self.device)
        return predicted_logits[(slice(None), *slicer_revert_padding[1:])]


def empty_cache(device: torch.device):
    if device.type == "cuda":
        torch.cuda.empty_cache()
    elif device.type == "mps":
        from torch import mps

        mps.empty_cache()
    else:
        pass


class dummy_context:
    def __enter__(self):
        pass

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass
