import argparse

from pprint import pformat
from torch.onnx.symbolic_helper import parse_args
import torch

import torchreid
from torchreid.ops import DataParallel
from torchreid.utils import (load_pretrained_weights, check_isfile,
                                resume_from_checkpoint, get_model_attr)

from scripts.default_config import (imagedata_kwargs, videodata_kwargs,
                                    get_default_config, model_kwargs,
                                    optimizer_kwargs, lr_scheduler_kwargs,
                                    merge_from_files_with_base)

def build_base_argparser():
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--config-file', type=str, default='',
                        help='path to config file')
    parser.add_argument('-s', '--sources', type=str, nargs='+',
                        help='source datasets (delimited by space)')
    parser.add_argument('-t', '--targets', type=str, nargs='+',
                        help='target datasets (delimited by space)')
    parser.add_argument('--root', type=str, default='',
                        help='path to data root')
    parser.add_argument('--classes', type=str, nargs='+',
                        help='name of classes in classification dataset')
    parser.add_argument('--custom-roots', type=str, nargs='+',
                        help='types or paths to annotation of custom datasets (delimited by space)')
    parser.add_argument('--custom-types', type=str, nargs='+',
                        help='path of custom datasets (delimited by space)')
    parser.add_argument('--custom-names', type=str, nargs='+',
                        help='names of custom datasets (delimited by space)')
    parser.add_argument('--gpu-num', type=int, default=1,
                        help='Number of GPUs for training. 0 is for CPU mode')
    parser.add_argument('opts', default=None, nargs=argparse.REMAINDER,
                        help='Modify config options using the command-line')
    return parser


def reset_config(cfg, args):
    if args.root:
        cfg.data.root = args.root

    if args.sources:
        cfg.data.sources = args.sources
    if args.targets:
        cfg.data.targets = args.targets

    if args.custom_roots:
        cfg.custom_datasets.roots = args.custom_roots
    if args.custom_types:
        cfg.custom_datasets.types = args.custom_types
    if args.custom_names:
        cfg.custom_datasets.names = args.custom_names

    if hasattr(args, 'auxiliary_models_cfg') and args.auxiliary_models_cfg:
        cfg.mutual_learning.aux_configs = args.auxiliary_models_cfg


def build_datamanager(cfg, classification_classes_filter=None):
    if cfg.data.type == 'image':
        return torchreid.data.ImageDataManager(filter_classes=classification_classes_filter, **imagedata_kwargs(cfg))
    else:
        return torchreid.data.VideoDataManager(**videodata_kwargs(cfg))


def build_auxiliary_model(config_file, num_classes, use_gpu, device_ids=None, lr=None,
                          nncf_aux_config_file=None,
                          aux_config_opts=None):
    aux_cfg = get_default_config()
    aux_cfg.use_gpu = use_gpu
    merge_from_files_with_base(aux_cfg, config_file)
    if nncf_aux_config_file:
        print(f'applying to aux config changes from NNCF aux config file {nncf_aux_config_file}')
        merge_from_files_with_base(aux_cfg, nncf_aux_config_file)
    if aux_config_opts:
        print(f'applying to aux config changes from command line arguments, '
                f'the changes are:\n{pformat(aux_config_opts)}')
        aux_cfg.merge_from_list(aux_config_opts)

    print('\nShow auxiliary configuration\n{}\n'.format(aux_cfg))

    if lr is not None:
        aux_cfg.train.lr = lr
        print(f"setting learning rate from main model: {lr}")
    model = torchreid.models.build_model(**model_kwargs(aux_cfg, num_classes))
    optimizer = torchreid.optim.build_optimizer(model, **optimizer_kwargs(aux_cfg))
    scheduler = torchreid.optim.build_lr_scheduler(optimizer, **lr_scheduler_kwargs(aux_cfg))

    if aux_cfg.model.resume and check_isfile(aux_cfg.model.resume):
        aux_cfg.train.start_epoch = resume_from_checkpoint(
            aux_cfg.model.resume, model, optimizer=optimizer, scheduler=scheduler)

    elif aux_cfg.model.load_weights and check_isfile(aux_cfg.model.load_weights):
        load_pretrained_weights(model, aux_cfg.model.load_weights)

    if aux_cfg.use_gpu:
        assert device_ids is not None

        if len(device_ids) > 1:
            model = DataParallel(model, device_ids=device_ids, output_device=0).cuda(device_ids[0])
        else:
            model = model.cuda(device_ids[0])

    return model, optimizer, scheduler


@parse_args('v', 'i', 'v', 'v', 'f', 'i')
def group_norm_symbolic(g, input_blob, num_groups, weight, bias, eps, cudnn_enabled):
    from torch.onnx.symbolic_opset9 import reshape, mul, add, reshape_as

    channels_num = input_blob.type().sizes()[1]

    if num_groups == channels_num:
        output = g.op('InstanceNormalization', input_blob, weight, bias, epsilon_f=eps)
    else:
        # Reshape from [n, g * cg, h, w] to [1, n * g, cg * h, w].
        x = reshape(g, input_blob, [0, num_groups, -1, 0])
        x = reshape(g, x, [1, -1, 0, 0])
        # Normalize channel-wise.
        x = g.op('MeanVarianceNormalization', x, axes_i=[2, 3])
        # Reshape back.
        x = reshape_as(g, x, input_blob)
        # Apply affine transform.
        x = mul(g, x, reshape(g, weight, [1, channels_num, 1, 1]))
        output = add(g, x, reshape(g, bias, [1, channels_num, 1, 1]))

    return output


def is_config_parameter_set_from_command_line(cmd_line_opts, parameter_name):
    # Note that cmd_line_opts here should be compatible with
    # the function yacs.config.CfgNode.merge_from_list
    if not cmd_line_opts:
        return False
    key_names = cmd_line_opts[0::2]
    return (parameter_name in key_names)


def put_on_the_device(model, use_gpu=True, gpu_num=1, num_aux_models=0, split_models=False):
    if use_gpu:
        num_devices = min(torch.cuda.device_count(), gpu_num)
        if num_aux_models > 0 and split_models:
            num_models = num_aux_models + 1
            assert num_devices >= num_models
            assert num_devices % num_models == 0

            num_devices_per_model = num_devices // num_models
            device_splits = []
            for model_id in range(num_models):
                device_splits.append([
                    model_id * num_devices_per_model + i
                    for i in range(num_devices_per_model)
                ])

            main_device_ids = device_splits[0]
            extra_device_ids = device_splits[1:]
        else:
            main_device_ids = list(range(num_devices))
            extra_device_ids = [main_device_ids for _ in range(num_aux_models)]

        if num_devices > 1:
            model = DataParallel(model, device_ids=main_device_ids, output_device=0).cuda(main_device_ids[0])
        else:
            model = model.cuda(main_device_ids[0])
    else:
        extra_device_ids = [None for _ in range()]

    return model, extra_device_ids


def check_classification_classes(model, datamanager, classes, test_only=False):
    def check_classes_consistency(ref_classes, probe_classes, strict=False):
        if strict:
            if len(ref_classes) != len(probe_classes):
                return False
            return sorted(probe_classes.keys()) == sorted(ref_classes.keys())
        else:
            if len(ref_classes) > len(probe_classes):
                return False
            probe_names = probe_classes.keys()
            for cl in ref_classes.keys():
                if cl not in probe_names:
                    return False
        return True

    classes_map = {v : k for k, v in enumerate(sorted(classes))} if classes else {}
    if test_only:
        for name, dataloader in datamanager.test_loader.items():
            if not len(dataloader['query'].dataset.classes): # current text annotation doesn't contain classes names
                print(f'Warning: classes are not defined for validation dataset {name}')
                continue
            if not len(get_model_attr(model, 'classification_classes')):
                print(f'Warning: classes are not provided in the current snapshot. Consistency checks are skipped.')
                continue
            if not check_classes_consistency(get_model_attr(model, 'classification_classes'),
                                                dataloader['query'].dataset.classes, strict=False):
                raise ValueError('Inconsistent classes in evaluation dataset')
            if classes and not check_classes_consistency(classes_map,
                                                                get_model_attr(model, 'classification_classes'), strict=True):
                raise ValueError('Classes provided via --classes should be the same as in the loaded model')
    elif classes:
        if not check_classes_consistency(classes_map,
                                            datamanager.train_loader.dataset.classes, strict=True):
            raise ValueError('Inconsistent classes in training dataset')
