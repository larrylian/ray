import logging
from typing import (
    Any,
    Mapping,
    Union,
    Type,
    Sequence,
    Hashable,
    Optional,
    Callable,
    Set,
)

from ray.rllib.core.rl_module.rl_module import (
    RLModule,
    ModuleID,
    SingleAgentRLModuleSpec,
)
from ray.rllib.core.rl_module.marl_module import MultiAgentRLModule
from ray.rllib.core.rl_trainer.rl_trainer import (
    FrameworkHPs,
    RLTrainer,
    ParamOptimizerPairs,
    Optimizer,
    ParamType,
    ParamDictType,
)
from ray.rllib.core.rl_module.torch.torch_rl_module import TorchDDPRLModule
from ray.rllib.policy.sample_batch import MultiAgentBatch
from ray.rllib.utils.annotations import override
from ray.rllib.utils.torch_utils import convert_to_torch_tensor
from ray.rllib.utils.numpy import convert_to_numpy
from ray.rllib.utils.typing import TensorType
from ray.rllib.utils.nested_dict import NestedDict
from ray.rllib.utils.framework import try_import_torch

torch, nn = try_import_torch()

if torch:
    from ray.train.torch.train_loop_utils import _TorchAccelerator


logger = logging.getLogger(__name__)


class TorchRLTrainer(RLTrainer):
    framework: str = "torch"

    def __init__(
        self,
        *,
        framework_hyperparameters: Optional[FrameworkHPs] = FrameworkHPs(),
        **kwargs,
    ):
        super().__init__(**kwargs)

        # will be set during build
        self._device = None

    @override(RLTrainer)
    def configure_optimizers(self) -> ParamOptimizerPairs:
        """Configures the optimizers for the Learner.

        By default it sets up a single Adam optimizer for each sub-module in module
        accessible via `moduel.keys()`.
        """
        # TODO (Kourosh): convert optimizer_config to dataclass later.
        lr = self._optimizer_config["lr"]
        return [
            (
                self.get_parameters(self._module[key]),
                torch.optim.Adam(self.get_parameters(self._module[key]), lr=lr),
            )
            for key in self._module.keys()
        ]

    @override(RLTrainer)
    def compute_gradients(
        self, loss: Union[TensorType, Mapping[str, Any]]
    ) -> ParamDictType:
        for optim in self._optim_to_param:
            # set_to_none is a faster way to zero out the gradients
            optim.zero_grad(set_to_none=True)
        loss[self.TOTAL_LOSS_KEY].backward()
        grads = {pid: p.grad for pid, p in self._params.items()}

        return grads

    @override(RLTrainer)
    def apply_gradients(self, gradients: ParamDictType) -> None:
        # make sure the parameters do not carry gradients on their own
        for optim in self._optim_to_param:
            optim.zero_grad(set_to_none=True)

        # set the gradient of the parameters
        for pid, grad in gradients.items():
            self._params[pid].grad = grad

        # for each optimizer call its step function with the gradients
        for optim in self._optim_to_param:
            optim.step()

    @override(RLTrainer)
    def get_weights(self, module_ids: Optional[Set[str]] = None) -> Mapping[str, Any]:
        """Returns the weights of the underlying MultiAgentRLModule"""
        module_weights = self._module.get_state()
        if module_ids is None:
            return module_weights

        return convert_to_numpy(
            {k: v for k, v in module_weights.items() if k in module_ids}
        )

    @override(RLTrainer)
    def set_weights(self, weights: Mapping[str, Any]) -> None:
        """Sets the weights of the underlying MultiAgentRLModule"""
        weights = convert_to_torch_tensor(weights, device=self._device)
        return self._module.set_state(weights)

    @override(RLTrainer)
    def get_param_ref(self, param: ParamType) -> Hashable:
        return param

    @override(RLTrainer)
    def get_parameters(self, module: RLModule) -> Sequence[ParamType]:
        return list(module.parameters())

    @override(RLTrainer)
    def get_optimizer_obj(
        self, module: RLModule, optimizer_cls: Type[Optimizer]
    ) -> Optimizer:
        # TODO (Kourosh): the abstraction should take in optimizer_config as a
        # parameter as well.
        lr = self._optimizer_config["lr"]
        return optimizer_cls(module.parameters(), lr=lr)

    @override(RLTrainer)
    def _convert_batch_type(self, batch: MultiAgentBatch):
        batch = convert_to_torch_tensor(batch.policy_batches, device=self._device)
        batch = NestedDict(batch)
        return batch

    @override(RLTrainer)
    def add_module(
        self,
        *,
        module_id: ModuleID,
        module_spec: SingleAgentRLModuleSpec,
        set_optimizer_fn: Optional[Callable[[RLModule], ParamOptimizerPairs]] = None,
        optimizer_cls: Optional[Type[Optimizer]] = None,
    ) -> None:
        super().add_module(
            module_id=module_id,
            module_spec=module_spec,
            set_optimizer_fn=set_optimizer_fn,
            optimizer_cls=optimizer_cls,
        )

        # we need to ddpify the module that was just added to the pool
        self._module[module_id].to(self._device)
        if self.distributed:
            self._module.add_module(
                module_id, TorchDDPRLModule(self._module[module_id]), override=True
            )

    @override(RLTrainer)
    def build(self) -> None:
        """Builds the TorchLearner.

        This method is specific to TorchLearner. Before running super() it will
        initialzed the device properly based on use_gpu and distributed flags, so that
        _make_module() can place the created module on the correct device. After
        running super() it will wrap the module in a TorchDDPRLModule if distributed is
        set.
        """
        # TODO (Kourosh): How do we handle model parallism?
        # TODO (Kourosh): Instead of using _TorchAccelerator, we should use the public
        # api in ray.train but allow for session to be None without any errors raised.
        if self._use_gpu:
            # _TorchAccelerator().get_device() returns the 0th device if
            # it is called from outside of a Ray Train session. Its necessary to give
            # the user the option to run on the gpu of their choice, so we enable that
            # option here via the local gpu id scaling config parameter.
            if self._distributed:
                self._device = _TorchAccelerator().get_device()
            else:
                assert self._local_gpu_idx < torch.cuda.device_count(), (
                    f"local_gpu_idx {self._local_gpu_idx} is not a valid GPU id or is "
                    " not available."
                )
                # this is an index into the available cuda devices. For example if
                # os.environ["CUDA_VISIBLE_DEVICES"] = "1" then
                # torch.cuda.device_count() = 1 and torch.device(0) will actuall map to
                # the gpu with id 1 on the node.
                self._device = torch.device(self._local_gpu_idx)
        else:
            self._device = torch.device("cpu")

        super().build()
        # if the module is a MultiAgentRLModule and nn.Module we can simply assume
        # all the submodules are registered. Otherwise, we need to loop through
        # each submodule and move it to the correct device.
        # TODO (Kourosh): This can result in missing modules if the user does not
        # register them in the MultiAgentRLModule. We should find a better way to
        # handle this.
        if self._distributed:
            if isinstance(self._module, torch.nn.Module):
                self._module = TorchDDPRLModule(self._module)
            else:
                for key in self._module.keys():
                    self._module.add_module(
                        key, TorchDDPRLModule(self._module[key]), override=True
                    )

    @override(RLTrainer)
    def _make_module(self) -> MultiAgentRLModule:
        module = super()._make_module()
        self._map_module_to_device(module)
        return module

    def _map_module_to_device(self, module: MultiAgentRLModule) -> None:
        """Moves the module to the correct device."""
        if isinstance(module, torch.nn.Module):
            module.to(self._device)
        else:
            for key in module.keys():
                module[key].to(self._device)
