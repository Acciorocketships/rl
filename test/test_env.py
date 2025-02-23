# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import argparse
import os.path
from collections import defaultdict
from functools import partial

import numpy as np
import pytest
import torch
import yaml

from _utils_internal import (
    _make_envs,
    CARTPOLE_VERSIONED,
    get_default_devices,
    HALFCHEETAH_VERSIONED,
    PENDULUM_VERSIONED,
    PONG_VERSIONED,
)
from mocking_classes import (
    ActionObsMergeLinear,
    ContinuousActionConvMockEnv,
    ContinuousActionConvMockEnvNumpy,
    ContinuousActionVecMockEnv,
    CountingBatchedEnv,
    CountingEnv,
    CountingEnvCountPolicy,
    DiscreteActionConvMockEnv,
    DiscreteActionConvMockEnvNumpy,
    DiscreteActionVecMockEnv,
    DummyModelBasedEnvBase,
    MockBatchedLockedEnv,
    MockBatchedUnLockedEnv,
    MockSerialEnv,
    NestedCountingEnv,
)
from packaging import version
from tensordict.nn import TensorDictModuleBase
from tensordict.tensordict import assert_allclose_td, TensorDict
from torch import nn

from torchrl.collectors import MultiSyncDataCollector, SyncDataCollector
from torchrl.data.tensor_specs import (
    CompositeSpec,
    OneHotDiscreteTensorSpec,
    UnboundedContinuousTensorSpec,
)
from torchrl.envs import CatTensors, DoubleToFloat, EnvCreator, ParallelEnv, SerialEnv
from torchrl.envs.gym_like import default_info_dict_reader
from torchrl.envs.libs.dm_control import _has_dmc, DMControlEnv
from torchrl.envs.libs.gym import _has_gym, GymEnv, GymWrapper
from torchrl.envs.transforms import Compose, StepCounter, TransformedEnv
from torchrl.envs.utils import check_env_specs, make_composite_from_td, step_mdp
from torchrl.modules import Actor, ActorCriticOperator, MLP, SafeModule, ValueOperator
from torchrl.modules.tensordict_module import WorldModelWrapper

gym_version = None
if _has_gym:
    try:
        import gymnasium as gym
    except ModuleNotFoundError:
        import gym

    gym_version = version.parse(gym.__version__)

try:
    this_dir = os.path.dirname(os.path.realpath(__file__))
    with open(os.path.join(this_dir, "configs", "atari.yaml"), "r") as file:
        atari_confs = yaml.load(file, Loader=yaml.FullLoader)
    _atari_found = True
except FileNotFoundError:
    _atari_found = False
    atari_confs = defaultdict(lambda: "")


## TO BE FIXED: DiscreteActionProjection queries a randint on each worker, which leads to divergent results between
## the serial and parallel batched envs
# def _make_atari_env(atari_env):
#     action_spec = GymEnv(atari_env + "-ram-v0").action_spec
#     n_act = action_spec.shape[-1]
#     return lambda **kwargs: TransformedEnv(
#         GymEnv(atari_env + "-ram-v0", **kwargs),
#         DiscreteActionProjection(max_N=18, M=n_act),
#     )
#
#
# @pytest.mark.skipif(
#     "ALE/Pong-v5" not in _get_gym_envs(), reason="no Atari OpenAI Gym env available"
# )
# def test_composite_env():
#     num_workers = 10
#     frameskip = 2
#     create_env_fn = [
#         _make_atari_env(atari_env)
#         for atari_env in atari_confs["atari_envs"][:num_workers]
#     ]
#     kwargs = {"frame_skip": frameskip}
#
#     random_policy = lambda td: td.set(
#         "action", torch.nn.functional.one_hot(torch.randint(18, (*td.batch_size,)), 18)
#     )
#     p = SerialEnv(num_workers, create_env_fn, create_env_kwargs=kwargs)
#     seed = p.set_seed(0)
#     p.reset()
#     torch.manual_seed(seed)
#     rollout1 = p.rollout(max_steps=100, policy=random_policy, auto_reset=False)
#     p.close()
#     del p
#
#     p = ParallelEnv(num_workers, create_env_fn, create_env_kwargs=kwargs)
#     seed = p.set_seed(0)
#     p.reset()
#     torch.manual_seed(seed)
#     rollout0 = p.rollout(max_steps=100, policy=random_policy, auto_reset=False)
#     p.close()
#     del p
#
#     assert_allclose_td(rollout1, rollout0)


@pytest.mark.skipif(not _has_gym, reason="no gym")
@pytest.mark.parametrize("env_name", [PENDULUM_VERSIONED, CARTPOLE_VERSIONED])
@pytest.mark.parametrize("frame_skip", [1, 4])
def test_env_seed(env_name, frame_skip, seed=0):
    env = GymEnv(env_name, frame_skip=frame_skip)
    action = env.action_spec.rand()

    env.set_seed(seed)
    td0a = env.reset()
    td1a = env.step(td0a.clone().set("action", action))

    env.set_seed(seed)
    td0b = env.fake_tensordict()
    td0b = env.reset(tensordict=td0b)
    td1b = env.step(td0b.clone().set("action", action))

    assert_allclose_td(td0a, td0b.select(*td0a.keys()))
    assert_allclose_td(td1a, td1b)

    env.set_seed(
        seed=seed + 10,
    )
    td0c = env.reset()
    td1c = env.step(td0c.clone().set("action", action))

    with pytest.raises(AssertionError):
        assert_allclose_td(td0a, td0c.select(*td0a.keys()))
    with pytest.raises(AssertionError):
        assert_allclose_td(td1a, td1c)
    env.close()


@pytest.mark.skipif(not _has_gym, reason="no gym")
@pytest.mark.parametrize("env_name", [PENDULUM_VERSIONED, PONG_VERSIONED])
@pytest.mark.parametrize("frame_skip", [1, 4])
def test_rollout(env_name, frame_skip, seed=0):
    env = GymEnv(env_name, frame_skip=frame_skip)

    torch.manual_seed(seed)
    np.random.seed(seed)
    env.set_seed(seed)
    env.reset()
    rollout1 = env.rollout(max_steps=100)
    assert rollout1.names[-1] == "time"

    torch.manual_seed(seed)
    np.random.seed(seed)
    env.set_seed(seed)
    env.reset()
    rollout2 = env.rollout(max_steps=100)
    assert rollout2.names[-1] == "time"

    assert_allclose_td(rollout1, rollout2)

    torch.manual_seed(seed)
    env.set_seed(seed + 10)
    env.reset()
    rollout3 = env.rollout(max_steps=100)
    with pytest.raises(AssertionError):
        assert_allclose_td(rollout1, rollout3)
    env.close()


@pytest.mark.parametrize("device", get_default_devices())
def test_rollout_predictability(device):
    env = MockSerialEnv(device=device)
    env.set_seed(100)
    first = 100 % 17
    policy = Actor(torch.nn.Linear(1, 1, bias=False)).to(device)
    for p in policy.parameters():
        p.data.fill_(1.0)
    td_out = env.rollout(policy=policy, max_steps=200)
    assert (
        torch.arange(first, first + 100, device=device)
        == td_out.get("observation").squeeze()
    ).all()
    assert (
        torch.arange(first + 1, first + 101, device=device)
        == td_out.get(("next", "observation")).squeeze()
    ).all()
    assert (
        torch.arange(first + 1, first + 101, device=device)
        == td_out.get(("next", "reward")).squeeze()
    ).all()
    assert (
        torch.arange(first, first + 100, device=device)
        == td_out.get("action").squeeze()
    ).all()


@pytest.mark.skipif(not _has_gym, reason="no gym")
@pytest.mark.parametrize(
    "env_name",
    [
        PENDULUM_VERSIONED,
    ],
)
@pytest.mark.parametrize(
    "frame_skip",
    [
        1,
    ],
)
@pytest.mark.parametrize("truncated_key", ["truncated", "done"])
@pytest.mark.parametrize("parallel", [True, False])
def test_rollout_reset(env_name, frame_skip, parallel, truncated_key, seed=0):
    envs = []
    for horizon in [20, 30, 40]:
        envs.append(
            lambda horizon=horizon: TransformedEnv(
                GymEnv(env_name, frame_skip=frame_skip),
                StepCounter(horizon, truncated_key=truncated_key),
            )
        )
    if parallel:
        env = ParallelEnv(3, envs)
    else:
        env = SerialEnv(3, envs)
    env.set_seed(100)
    out = env.rollout(100, break_when_any_done=False)
    assert out.names[-1] == "time"
    assert out.shape == torch.Size([3, 100])
    assert (
        out["next", truncated_key].squeeze().sum(-1) == torch.tensor([5, 3, 2])
    ).all()


class TestModelBasedEnvBase:
    @staticmethod
    def world_model():
        return WorldModelWrapper(
            SafeModule(
                ActionObsMergeLinear(5, 4),
                in_keys=["hidden_observation", "action"],
                out_keys=["hidden_observation"],
            ),
            SafeModule(
                nn.Linear(4, 1),
                in_keys=["hidden_observation"],
                out_keys=["reward"],
            ),
        )

    @pytest.mark.parametrize("device", get_default_devices())
    def test_mb_rollout(self, device, seed=0):
        torch.manual_seed(seed)
        np.random.seed(seed)
        world_model = self.world_model()
        mb_env = DummyModelBasedEnvBase(
            world_model, device=device, batch_size=torch.Size([10])
        )
        check_env_specs(mb_env)
        rollout = mb_env.rollout(max_steps=100)
        expected_keys = {
            ("next", key) for key in (*mb_env.observation_spec.keys(), "reward", "done")
        }
        expected_keys = expected_keys.union(
            set(mb_env.input_spec["_action_spec"].keys())
        )
        expected_keys = expected_keys.union(
            set(mb_env.input_spec["_state_spec"].keys())
        )
        expected_keys = expected_keys.union({"done", "next"})
        assert set(rollout.keys(True)) == expected_keys
        assert rollout[("next", "hidden_observation")].shape == (10, 100, 4)

    @pytest.mark.parametrize("device", get_default_devices())
    def test_mb_env_batch_lock(self, device, seed=0):
        torch.manual_seed(seed)
        np.random.seed(seed)
        world_model = WorldModelWrapper(
            SafeModule(
                ActionObsMergeLinear(5, 4),
                in_keys=["hidden_observation", "action"],
                out_keys=["hidden_observation"],
            ),
            SafeModule(
                nn.Linear(4, 1),
                in_keys=["hidden_observation"],
                out_keys=["reward"],
            ),
        )
        mb_env = DummyModelBasedEnvBase(
            world_model, device=device, batch_size=torch.Size([10])
        )
        assert not mb_env.batch_locked

        with pytest.raises(RuntimeError, match="batch_locked is a read-only property"):
            mb_env.batch_locked = False
        td = mb_env.reset()
        td["action"] = mb_env.action_spec.rand()
        td_expanded = td.unsqueeze(-1).expand(10, 2).reshape(-1).to_tensordict()
        mb_env.step(td)

        with pytest.raises(RuntimeError, match="Expected a tensordict with shape"):
            mb_env.step(td_expanded)

        mb_env = DummyModelBasedEnvBase(
            world_model, device=device, batch_size=torch.Size([])
        )
        assert not mb_env.batch_locked

        with pytest.raises(RuntimeError, match="batch_locked is a read-only property"):
            mb_env.batch_locked = False
        td = mb_env.reset()
        td["action"] = mb_env.action_spec.rand()
        td_expanded = td.expand(2)
        mb_env.step(td)
        # we should be able to do a step with a tensordict that has been expended
        mb_env.step(td_expanded)


class TestParallel:
    @pytest.mark.parametrize("num_parallel_env", [1, 10])
    @pytest.mark.parametrize("env_batch_size", [[], (32,), (32, 1), (32, 0)])
    def test_env_with_batch_size(self, num_parallel_env, env_batch_size):
        env = MockBatchedLockedEnv(device="cpu", batch_size=torch.Size(env_batch_size))
        env.set_seed(1)
        parallel_env = ParallelEnv(num_parallel_env, lambda: env)
        assert parallel_env.batch_size == (num_parallel_env, *env_batch_size)

    @pytest.mark.skipif(not _has_dmc, reason="no dm_control")
    @pytest.mark.parametrize("env_task", ["stand,stand,stand", "stand,walk,stand"])
    @pytest.mark.parametrize("share_individual_td", [True, False])
    def test_multi_task_serial_parallel(self, env_task, share_individual_td):
        tasks = env_task.split(",")
        if len(tasks) == 1:
            single_task = True

            def env_make():
                return DMControlEnv("humanoid", tasks[0])

        elif len(set(tasks)) == 1 and len(tasks) == 3:
            single_task = True
            env_make = [lambda: DMControlEnv("humanoid", tasks[0])] * 3
        else:
            single_task = False
            env_make = [
                lambda task=task: DMControlEnv("humanoid", task) for task in tasks
            ]

        if not share_individual_td and not single_task:
            with pytest.raises(
                ValueError, match="share_individual_td must be set to None"
            ):
                SerialEnv(3, env_make, share_individual_td=share_individual_td)
            with pytest.raises(
                ValueError, match="share_individual_td must be set to None"
            ):
                ParallelEnv(3, env_make, share_individual_td=share_individual_td)
            return

        env_serial = SerialEnv(3, env_make, share_individual_td=share_individual_td)
        env_serial.start()
        assert env_serial._single_task is single_task
        env_parallel = ParallelEnv(3, env_make, share_individual_td=share_individual_td)
        env_parallel.start()
        assert env_parallel._single_task is single_task

        env_serial.set_seed(0)
        torch.manual_seed(0)
        td_serial = env_serial.rollout(max_steps=50)

        env_parallel.set_seed(0)
        torch.manual_seed(0)
        td_parallel = env_parallel.rollout(max_steps=50)

        assert_allclose_td(td_serial, td_parallel)

    @pytest.mark.skipif(not _has_dmc, reason="no dm_control")
    def test_multitask(self):
        env1 = DMControlEnv("humanoid", "stand")
        env1_obs_keys = list(env1.observation_spec.keys())
        env2 = DMControlEnv("humanoid", "walk")
        env2_obs_keys = list(env2.observation_spec.keys())

        assert len(env1_obs_keys)
        assert len(env2_obs_keys)

        def env1_maker():
            return TransformedEnv(
                DMControlEnv("humanoid", "stand"),
                Compose(
                    CatTensors(env1_obs_keys, "observation_stand", del_keys=False),
                    CatTensors(env1_obs_keys, "observation"),
                    DoubleToFloat(
                        in_keys=["observation_stand", "observation"],
                        in_keys_inv=["action"],
                    ),
                ),
            )

        def env2_maker():
            return TransformedEnv(
                DMControlEnv("humanoid", "walk"),
                Compose(
                    CatTensors(env2_obs_keys, "observation_walk", del_keys=False),
                    CatTensors(env2_obs_keys, "observation"),
                    DoubleToFloat(
                        in_keys=["observation_walk", "observation"],
                        in_keys_inv=["action"],
                    ),
                ),
            )

        env = ParallelEnv(2, [env1_maker, env2_maker])
        # env = SerialEnv(2, [env1_maker, env2_maker])
        assert not env._single_task

        td = env.rollout(10, return_contiguous=False)
        assert "observation_walk" not in td.keys()
        assert "observation_walk" in td[1].keys()
        assert "observation_walk" not in td[0].keys()
        assert "observation_stand" in td[0].keys()
        assert "observation_stand" not in td[1].keys()
        assert "observation_walk" in td[:, 0][1].keys()
        assert "observation_walk" not in td[:, 0][0].keys()
        assert "observation_stand" in td[:, 0][0].keys()
        assert "observation_stand" not in td[:, 0][1].keys()

    @pytest.mark.skipif(not _has_gym, reason="no gym")
    @pytest.mark.parametrize("env_name", [PENDULUM_VERSIONED])  # 1226: faster execution
    @pytest.mark.parametrize("frame_skip", [4])  # 1226: faster execution
    @pytest.mark.parametrize(
        "transformed_in,transformed_out", [[True, True], [False, False]]
    )  # 1226: faster execution
    def test_parallel_env(
        self, env_name, frame_skip, transformed_in, transformed_out, T=10, N=3
    ):
        env_parallel, env_serial, _, env0 = _make_envs(
            env_name,
            frame_skip,
            transformed_in=transformed_in,
            transformed_out=transformed_out,
            N=N,
        )
        td = TensorDict(
            source={"action": env0.action_spec.rand((N,))},
            batch_size=[
                N,
            ],
        )
        td1 = env_parallel.step(td)
        assert not td1.is_shared()
        assert ("next", "done") in td1.keys(True)
        assert ("next", "reward") in td1.keys(True)

        with pytest.raises(RuntimeError):
            # number of actions does not match number of workers
            td = TensorDict(
                source={"action": env0.action_spec.rand((N - 1,))},
                batch_size=[N - 1],
            )
            _ = env_parallel.step(td)

        td_reset = TensorDict(
            source={"_reset": env_parallel.done_spec.rand()},
            batch_size=[
                N,
            ],
        )
        env_parallel.reset(tensordict=td_reset)

        td = env_parallel.rollout(policy=None, max_steps=T)
        assert (
            td.shape == torch.Size([N, T]) or td.get("done").sum(1).all()
        ), f"{td.shape}, {td.get('done').sum(1)}"
        env_parallel.close()
        # env_serial.close()  # never opened
        env0.close()

    @pytest.mark.skipif(not _has_gym, reason="no gym")
    @pytest.mark.parametrize("env_name", [PENDULUM_VERSIONED])
    @pytest.mark.parametrize("frame_skip", [4])  # 1226: faster execution
    @pytest.mark.parametrize(
        "transformed_in,transformed_out", [[True, True], [False, False]]
    )  # 1226: faster execution
    def test_parallel_env_with_policy(
        self,
        env_name,
        frame_skip,
        transformed_in,
        transformed_out,
        T=10,
        N=3,
    ):
        env_parallel, env_serial, _, env0 = _make_envs(
            env_name,
            frame_skip,
            transformed_in=transformed_in,
            transformed_out=transformed_out,
            N=N,
        )

        policy = ActorCriticOperator(
            SafeModule(
                spec=None,
                module=nn.LazyLinear(12),
                in_keys=["observation"],
                out_keys=["hidden"],
            ),
            SafeModule(
                spec=None,
                module=nn.LazyLinear(env0.action_spec.shape[-1]),
                in_keys=["hidden"],
                out_keys=["action"],
            ),
            ValueOperator(
                module=MLP(out_features=1, num_cells=[]), in_keys=["hidden", "action"]
            ),
        )

        td = TensorDict(
            source={"action": env0.action_spec.rand((N,))},
            batch_size=[
                N,
            ],
        )
        td1 = env_parallel.step(td)
        assert not td1.is_shared()
        assert ("next", "done") in td1.keys(True)
        assert ("next", "reward") in td1.keys(True)

        with pytest.raises(RuntimeError):
            # number of actions does not match number of workers
            td = TensorDict(
                source={"action": env0.action_spec.rand((N - 1,))},
                batch_size=[N - 1],
            )
            _ = env_parallel.step(td)

        td_reset = TensorDict(
            source={"_reset": env_parallel.done_spec.rand()},
            batch_size=[
                N,
            ],
        )
        env_parallel.reset(tensordict=td_reset)

        td = env_parallel.rollout(policy=policy, max_steps=T)
        assert (
            td.shape == torch.Size([N, T]) or td.get("done").sum(1).all()
        ), f"{td.shape}, {td.get('done').sum(1)}"
        env_parallel.close()

        # env_serial.close()
        env0.close()

    @pytest.mark.skipif(not _has_gym, reason="no gym")
    @pytest.mark.parametrize(
        "env_name",
        [
            PENDULUM_VERSIONED,
        ],
    )  # PONG_VERSIONED])  # 1226: efficiency
    @pytest.mark.parametrize("frame_skip", [4])
    @pytest.mark.parametrize(
        "transformed_in,transformed_out", [[True, True], [False, False]]
    )  # 1226: effociency
    @pytest.mark.parametrize("static_seed", [False, True])
    def test_parallel_env_seed(
        self, env_name, frame_skip, transformed_in, transformed_out, static_seed
    ):
        env_parallel, env_serial, _, _ = _make_envs(
            env_name, frame_skip, transformed_in, transformed_out, 5
        )
        out_seed_serial = env_serial.set_seed(0, static_seed=static_seed)
        if static_seed:
            assert out_seed_serial == 0
        td0_serial = env_serial.reset()
        torch.manual_seed(0)

        td_serial = env_serial.rollout(
            max_steps=10, auto_reset=False, tensordict=td0_serial
        ).contiguous()
        key = "pixels" if "pixels" in td_serial.keys() else "observation"
        torch.testing.assert_close(
            td_serial[:, 0].get(("next", key)), td_serial[:, 1].get(key)
        )

        out_seed_parallel = env_parallel.set_seed(0, static_seed=static_seed)
        if static_seed:
            assert out_seed_serial == 0
        td0_parallel = env_parallel.reset()

        torch.manual_seed(0)
        assert out_seed_parallel == out_seed_serial
        td_parallel = env_parallel.rollout(
            max_steps=10, auto_reset=False, tensordict=td0_parallel
        ).contiguous()
        torch.testing.assert_close(
            td_parallel[:, :-1].get(("next", key)), td_parallel[:, 1:].get(key)
        )
        assert_allclose_td(td0_serial, td0_parallel)
        assert_allclose_td(td_serial[:, 0], td_parallel[:, 0])  # first step
        assert_allclose_td(td_serial[:, 1], td_parallel[:, 1])  # second step
        assert_allclose_td(td_serial, td_parallel)
        env_parallel.close()
        env_serial.close()

    @pytest.mark.skipif(not _has_gym, reason="no gym")
    def test_parallel_env_shutdown(self):
        env_make = EnvCreator(lambda: GymEnv(PENDULUM_VERSIONED))
        env = ParallelEnv(4, env_make)
        env.reset()
        assert not env.is_closed
        env.rand_step()
        assert not env.is_closed
        env.close()
        assert env.is_closed
        env.reset()
        assert not env.is_closed
        env.close()

    @pytest.mark.parametrize("parallel", [True, False])
    def test_parallel_env_custom_method(self, parallel):
        # define env

        if parallel:
            env = ParallelEnv(2, lambda: DiscreteActionVecMockEnv())
        else:
            env = SerialEnv(2, lambda: DiscreteActionVecMockEnv())

        # we must start the environment first
        env.reset()
        assert all(result == 0 for result in env.custom_fun())
        assert all(result == 1 for result in env.custom_attr)
        assert all(result == 2 for result in env.custom_prop)  # to be fixed
        env.close()

    @pytest.mark.skipif(not torch.cuda.device_count(), reason="no cuda to test on")
    @pytest.mark.skipif(not _has_gym, reason="no gym")
    @pytest.mark.parametrize("frame_skip", [4])
    @pytest.mark.parametrize("device", [0])
    @pytest.mark.parametrize(
        "env_name", [PENDULUM_VERSIONED]
    )  # 1226: Skip PONG for efficiency
    @pytest.mark.parametrize(
        "transformed_in,transformed_out,open_before",
        [  # 1226: efficiency
            [True, True, True],
            [True, True, False],
            [False, False, True],
        ],
    )
    def test_parallel_env_cast(
        self,
        env_name,
        frame_skip,
        transformed_in,
        transformed_out,
        device,
        open_before,
        N=3,
    ):
        # tests casting to device
        env_parallel, env_serial, _, env0 = _make_envs(
            env_name,
            frame_skip,
            transformed_in=transformed_in,
            transformed_out=transformed_out,
            N=N,
        )
        if open_before:
            td_cpu = env0.rollout(max_steps=10)
            assert td_cpu.device == torch.device("cpu")
        env0 = env0.to(device)
        assert env0.observation_spec.device == torch.device(device)
        assert env0.action_spec.device == torch.device(device)
        assert env0.reward_spec.device == torch.device(device)
        assert env0.device == torch.device(device)
        td_device = env0.reset()
        assert td_device.device == torch.device(device), env0
        td_device = env0.rand_step()
        assert td_device.device == torch.device(device), env0
        td_device = env0.rollout(max_steps=10)
        assert td_device.device == torch.device(device), env0

        if open_before:
            td_cpu = env_serial.rollout(max_steps=10)
            assert td_cpu.device == torch.device("cpu")
        observation_spec = env_serial.observation_spec.clone()
        done_spec = env_serial.done_spec.clone()
        reward_spec = env_serial.reward_spec.clone()
        action_spec = env_serial.action_spec.clone()
        state_spec = env_serial.state_spec.clone()
        env_serial = env_serial.to(device)
        assert env_serial.observation_spec.device == torch.device(device)
        assert env_serial.action_spec.device == torch.device(device)
        assert env_serial.reward_spec.device == torch.device(device)
        assert env_serial.device == torch.device(device)
        assert env_serial.observation_spec == observation_spec.to(device)
        assert env_serial.action_spec == action_spec.to(device)
        assert env_serial.reward_spec == reward_spec.to(device)
        assert env_serial.done_spec == done_spec.to(device)
        assert env_serial.state_spec == state_spec.to(device)
        td_device = env_serial.reset()
        assert td_device.device == torch.device(device), env_serial
        td_device = env_serial.rand_step()
        assert td_device.device == torch.device(device), env_serial
        td_device = env_serial.rollout(max_steps=10)
        assert td_device.device == torch.device(device), env_serial

        if open_before:
            td_cpu = env_parallel.rollout(max_steps=10)
            assert td_cpu.device == torch.device("cpu")
        observation_spec = env_parallel.observation_spec.clone()
        done_spec = env_parallel.done_spec.clone()
        reward_spec = env_parallel.reward_spec.clone()
        action_spec = env_parallel.action_spec.clone()
        state_spec = env_parallel.state_spec.clone()
        env_parallel = env_parallel.to(device)
        assert env_parallel.observation_spec.device == torch.device(device)
        assert env_parallel.action_spec.device == torch.device(device)
        assert env_parallel.reward_spec.device == torch.device(device)
        assert env_parallel.device == torch.device(device)
        assert env_parallel.observation_spec == observation_spec.to(device)
        assert env_parallel.action_spec == action_spec.to(device)
        assert env_parallel.reward_spec == reward_spec.to(device)
        assert env_parallel.done_spec == done_spec.to(device)
        assert env_parallel.state_spec == state_spec.to(device)
        td_device = env_parallel.reset()
        assert td_device.device == torch.device(device), env_parallel
        td_device = env_parallel.rand_step()
        assert td_device.device == torch.device(device), env_parallel
        td_device = env_parallel.rollout(max_steps=10)
        assert td_device.device == torch.device(device), env_parallel

        env_parallel.close()
        env_serial.close()
        env0.close()

    @pytest.mark.skipif(not _has_gym, reason="no gym")
    @pytest.mark.skipif(not torch.cuda.device_count(), reason="no cuda device detected")
    @pytest.mark.parametrize("frame_skip", [4])
    @pytest.mark.parametrize("device", [0])
    @pytest.mark.parametrize("env_name", [PENDULUM_VERSIONED])  # 1226: efficiency
    @pytest.mark.parametrize(
        "transformed_in,transformed_out",
        [  # 1226
            [True, True],
            [False, False],
        ],
    )
    def test_parallel_env_device(
        self, env_name, frame_skip, transformed_in, transformed_out, device
    ):
        # tests creation on device
        torch.manual_seed(0)
        N = 3

        env_parallel, env_serial, _, env0 = _make_envs(
            env_name,
            frame_skip,
            transformed_in=transformed_in,
            transformed_out=transformed_out,
            device=device,
            N=N,
        )

        assert env0.device == torch.device(device)
        out = env0.rollout(max_steps=20)
        assert out.device == torch.device(device)

        assert env_serial.device == torch.device(device)
        out = env_serial.rollout(max_steps=20)
        assert out.device == torch.device(device)

        assert env_parallel.device == torch.device(device)
        out = env_parallel.rollout(max_steps=20)
        assert out.device == torch.device(device)

        env_parallel.close()
        env_serial.close()
        env0.close()

    @pytest.mark.skipif(not _has_gym, reason="no gym")
    @pytest.mark.flaky(reruns=3, reruns_delay=1)
    @pytest.mark.parametrize(
        "env_name", [PENDULUM_VERSIONED]
    )  # 1226: No pong for efficiency
    @pytest.mark.parametrize("frame_skip", [4])
    @pytest.mark.parametrize(
        "device",
        [torch.device("cuda:0") if torch.cuda.device_count() else torch.device("cpu")],
    )
    def test_parallel_env_transform_consistency(self, env_name, frame_skip, device):
        env_parallel_in, env_serial_in, _, env0_in = _make_envs(
            env_name,
            frame_skip,
            transformed_in=True,
            transformed_out=False,
            device=device,
            N=3,
        )
        env_parallel_out, env_serial_out, _, env0_out = _make_envs(
            env_name,
            frame_skip,
            transformed_in=False,
            transformed_out=True,
            device=device,
            N=3,
        )
        torch.manual_seed(0)
        env_parallel_in.set_seed(0)
        r_in = env_parallel_in.rollout(max_steps=20)
        torch.manual_seed(0)
        env_parallel_out.set_seed(0)
        r_out = env_parallel_out.rollout(max_steps=20)
        assert_allclose_td(r_in, r_out)
        env_parallel_in.close()
        env_parallel_out.close()

        torch.manual_seed(0)
        env_serial_in.set_seed(0)
        r_in = env_serial_in.rollout(max_steps=20)
        torch.manual_seed(0)
        env_serial_out.set_seed(0)
        r_out = env_serial_out.rollout(max_steps=20)
        assert_allclose_td(r_in, r_out)
        env_serial_in.close()
        env_serial_out.close()

        torch.manual_seed(0)
        env0_in.set_seed(0)
        r_in = env0_in.rollout(max_steps=20)
        torch.manual_seed(0)
        env0_out.set_seed(0)
        r_out = env0_out.rollout(max_steps=20)
        assert_allclose_td(r_in, r_out)
        env0_in.close()
        env0_in.close()

    @pytest.mark.parametrize("parallel", [True, False])
    def test_parallel_env_kwargs_set(self, parallel):
        num_env = 2

        def make_make_env():
            def make_transformed_env(seed=None):
                env = DiscreteActionConvMockEnv()
                if seed is not None:
                    env.set_seed(seed)
                return env

            return make_transformed_env

        _class = ParallelEnv if parallel else SerialEnv

        def env_fn1(seed):
            env = _class(
                num_workers=num_env,
                create_env_fn=make_make_env(),
                create_env_kwargs=[{"seed": i} for i in range(seed, seed + num_env)],
            )
            return env

        def env_fn2(seed):
            env = _class(
                num_workers=num_env,
                create_env_fn=make_make_env(),
            )
            env.update_kwargs([{"seed": i} for i in range(seed, seed + num_env)])
            return env

        env1 = env_fn1(100)
        env2 = env_fn2(100)
        env1.start()
        env2.start()
        for c1, c2 in zip(env1.counter, env2.counter):
            assert c1 == c2

        env1.close()
        env2.close()

    @pytest.mark.parametrize(
        "batch_size",
        [
            (32, 5),
            (4,),
            (1,),
            (),
        ],
    )
    @pytest.mark.parametrize("n_workers", [2, 1])
    def test_parallel_env_reset_flag(self, batch_size, n_workers, max_steps=3):
        torch.manual_seed(1)
        env = ParallelEnv(
            n_workers, lambda: CountingEnv(max_steps=max_steps, batch_size=batch_size)
        )
        env.set_seed(1)
        action = env.action_spec.rand()
        action[:] = 1
        for i in range(max_steps):
            td = env.step(
                TensorDict(
                    {"action": action}, batch_size=env.batch_size, device=env.device
                )
            )
            assert (td["next", "done"] == 0).all()
            assert (td["next"]["observation"] == i + 1).all()

        td = env.step(
            TensorDict({"action": action}, batch_size=env.batch_size, device=env.device)
        )
        assert (td["next", "done"] == 1).all()
        assert (td["next"]["observation"] == max_steps + 1).all()

        _reset = env.done_spec.rand()
        while not _reset.any():
            _reset = env.done_spec.rand()

        td_reset = env.reset(
            TensorDict({"_reset": _reset}, batch_size=env.batch_size, device=env.device)
        )
        env.close()

        assert (td_reset["done"][_reset] == 0).all()
        assert (td_reset["observation"][_reset] == 0).all()
        assert (td_reset["done"][~_reset] == 1).all()
        assert (td_reset["observation"][~_reset] == max_steps + 1).all()

    @pytest.mark.parametrize("nested_obs_action", [True, False])
    @pytest.mark.parametrize("nested_done", [True, False])
    @pytest.mark.parametrize("nested_reward", [True, False])
    @pytest.mark.parametrize("env_type", ["serial", "parallel"])
    def test_parallel_env_nested(
        self,
        nested_obs_action,
        nested_done,
        nested_reward,
        env_type,
        n_envs=2,
        batch_size=(32,),
        nested_dim=5,
        rollout_length=3,
        seed=1,
    ):
        env_fn = lambda: NestedCountingEnv(
            nest_done=nested_done,
            nest_reward=nested_reward,
            nest_obs_action=nested_obs_action,
            batch_size=batch_size,
            nested_dim=nested_dim,
        )
        if env_type == "serial":
            env = SerialEnv(n_envs, env_fn)
        else:
            env = ParallelEnv(n_envs, env_fn)
        env.set_seed(seed)

        batch_size = (n_envs, *batch_size)

        td = env.reset()
        assert td.batch_size == batch_size
        if nested_done or nested_obs_action:
            assert td["data"].batch_size == (*batch_size, nested_dim)
        if not nested_done and not nested_reward and not nested_obs_action:
            assert "data" not in td.keys()

        policy = CountingEnvCountPolicy(env.action_spec, env.action_key)
        td = env.rollout(rollout_length, policy)
        assert td.batch_size == (*batch_size, rollout_length)
        if nested_done or nested_obs_action:
            assert td["data"].batch_size == (*batch_size, rollout_length, nested_dim)
        if nested_reward or nested_done or nested_obs_action:
            assert td["next", "data"].batch_size == (
                *batch_size,
                rollout_length,
                nested_dim,
            )
        if not nested_done and not nested_reward and not nested_obs_action:
            assert "data" not in td.keys()
            assert "data" not in td["next"].keys()

        if nested_obs_action:
            assert "observation" not in td.keys()
            assert (td[..., -1]["data", "states"] == 2).all()
        else:
            assert ("data", "states") not in td.keys(True, True)
            assert (td[..., -1]["observation"] == 2).all()


@pytest.mark.parametrize("batch_size", [(), (2,), (32, 5)])
def test_env_base_reset_flag(batch_size, max_steps=3):
    env = CountingEnv(max_steps=max_steps, batch_size=batch_size)
    env.set_seed(1)

    action = env.action_spec.rand()
    action[:] = 1

    for i in range(max_steps):
        td = env.step(
            TensorDict({"action": action}, batch_size=env.batch_size, device=env.device)
        )
        assert (td["next", "done"] == 0).all()
        assert (td["next", "observation"] == i + 1).all()

    td = env.step(
        TensorDict({"action": action}, batch_size=env.batch_size, device=env.device)
    )
    assert (td["next", "done"] == 1).all()
    assert (td["next", "observation"] == max_steps + 1).all()

    _reset = env.done_spec.rand()
    td_reset = env.reset(
        TensorDict({"_reset": _reset}, batch_size=env.batch_size, device=env.device)
    )

    assert (td_reset["done"][_reset] == 0).all()
    assert (td_reset["observation"][_reset] == 0).all()
    assert (td_reset["done"][~_reset] == 1).all()
    assert (td_reset["observation"][~_reset] == max_steps + 1).all()


@pytest.mark.skipif(not _has_gym, reason="no gym")
def test_seed():
    torch.manual_seed(0)
    env1 = GymEnv(PENDULUM_VERSIONED)
    env1.set_seed(0)
    state0_1 = env1.reset()
    state1_1 = env1.step(state0_1.set("action", env1.action_spec.rand()))

    torch.manual_seed(0)
    env2 = GymEnv(PENDULUM_VERSIONED)
    env2.set_seed(0)
    state0_2 = env2.reset()
    state1_2 = env2.step(state0_2.set("action", env2.action_spec.rand()))

    assert_allclose_td(state0_1, state0_2)
    assert_allclose_td(state1_1, state1_2)

    env1.set_seed(0)
    torch.manual_seed(0)
    rollout1 = env1.rollout(max_steps=30)

    env2.set_seed(0)
    torch.manual_seed(0)
    rollout2 = env2.rollout(max_steps=30)

    torch.testing.assert_close(
        rollout1["observation"][1:], rollout1[("next", "observation")][:-1]
    )
    torch.testing.assert_close(
        rollout2["observation"][1:], rollout2[("next", "observation")][:-1]
    )
    torch.testing.assert_close(rollout1["observation"], rollout2["observation"])


class TestStepMdp:
    @pytest.mark.parametrize("keep_other", [True, False])
    @pytest.mark.parametrize("exclude_reward", [True, False])
    @pytest.mark.parametrize("exclude_done", [True, False])
    @pytest.mark.parametrize("exclude_action", [True, False])
    @pytest.mark.parametrize("has_out", [True, False])
    @pytest.mark.parametrize("lazy_stack", [False, True])
    def test_steptensordict(
        self,
        keep_other,
        exclude_reward,
        exclude_done,
        exclude_action,
        has_out,
        lazy_stack,
    ):
        torch.manual_seed(0)
        tensordict = TensorDict(
            {
                "reward": torch.randn(4, 1),
                "done": torch.zeros(4, 1, dtype=torch.bool),
                "ledzep": torch.randn(4, 2),
                "next": {
                    "ledzep": torch.randn(4, 2),
                    "reward": torch.randn(4, 1),
                    "done": torch.zeros(4, 1, dtype=torch.bool),
                },
                "beatles": torch.randn(4, 1),
                "action": torch.randn(4, 2),
            },
            [4],
        )
        if lazy_stack:
            # let's spice this a little bit
            tds = tensordict.unbind(0)
            tds[0]["this", "one"] = torch.zeros(2)
            tds[1]["but", "not", "this", "one"] = torch.ones(2)
            tds[0]["next", "this", "one"] = torch.ones(2) * 2
            tensordict = torch.stack(tds, 0)
        next_tensordict = TensorDict({}, [4]) if has_out else None
        if has_out and lazy_stack:
            next_tensordict = torch.stack(next_tensordict.unbind(0), 0)
        out = step_mdp(
            tensordict.lock_(),
            keep_other=keep_other,
            exclude_reward=exclude_reward,
            exclude_done=exclude_done,
            exclude_action=exclude_action,
            next_tensordict=next_tensordict,
        )
        assert "ledzep" in out.keys()
        if lazy_stack:
            assert (out["ledzep"] == tensordict["next", "ledzep"]).all()
            assert (out[0]["this", "one"] == 2).all()
            if keep_other:
                assert (out[1]["but", "not", "this", "one"] == 1).all()
        else:
            assert out["ledzep"] is tensordict["next", "ledzep"]
        if keep_other:
            assert "beatles" in out.keys()
            if lazy_stack:
                assert (out["beatles"] == tensordict["beatles"]).all()
            else:
                assert out["beatles"] is tensordict["beatles"]
        else:
            assert "beatles" not in out.keys()
        if not exclude_reward:
            assert "reward" in out.keys()
            if lazy_stack:
                assert (out["reward"] == tensordict["next", "reward"]).all()
            else:
                assert out["reward"] is tensordict["next", "reward"]
        else:
            assert "reward" not in out.keys()
        if not exclude_action:
            assert "action" in out.keys()
            if lazy_stack:
                assert (out["action"] == tensordict["action"]).all()
            else:
                assert out["action"] is tensordict["action"]
        else:
            assert "action" not in out.keys()
        if not exclude_done:
            assert "done" in out.keys()
            if lazy_stack:
                assert (out["done"] == tensordict["next", "done"]).all()
            else:
                assert out["done"] is tensordict["next", "done"]
        else:
            assert "done" not in out.keys()
        if has_out:
            assert out is next_tensordict

    @pytest.mark.parametrize("nested_obs", [True, False])
    @pytest.mark.parametrize("nested_action", [True, False])
    @pytest.mark.parametrize("nested_done", [True, False])
    @pytest.mark.parametrize("nested_reward", [True, False])
    @pytest.mark.parametrize("nested_other", [True, False])
    @pytest.mark.parametrize("exclude_reward", [True, False])
    @pytest.mark.parametrize("exclude_done", [True, False])
    @pytest.mark.parametrize("exclude_action", [True, False])
    @pytest.mark.parametrize("keep_other", [True, False])
    def test_nested(
        self,
        nested_obs,
        nested_action,
        nested_done,
        nested_reward,
        nested_other,
        exclude_reward,
        exclude_done,
        exclude_action,
        keep_other,
    ):
        td_batch_size = (4,)
        nested_batch_size = (4, 3)
        nested_key = ("data",)
        td = TensorDict(
            {
                nested_key: TensorDict({}, nested_batch_size),
                "next": {
                    nested_key: TensorDict({}, nested_batch_size),
                },
            },
            td_batch_size,
        )
        reward_key = "reward"
        if nested_reward:
            reward_key = nested_key + (reward_key,)
        done_key = "done"
        if nested_done:
            done_key = nested_key + (done_key,)
        action_key = "action"
        if nested_action:
            action_key = nested_key + (action_key,)
        obs_key = "state"
        if nested_obs:
            obs_key = nested_key + (obs_key,)
        other_key = "beatles"
        if nested_other:
            other_key = nested_key + (other_key,)

        td[reward_key] = torch.zeros(*nested_batch_size, 1)
        td[done_key] = torch.zeros(*nested_batch_size, 1)
        td[obs_key] = torch.zeros(*nested_batch_size, 1)
        td[action_key] = torch.zeros(*nested_batch_size, 1)
        td[other_key] = torch.zeros(*nested_batch_size, 1)

        td["next", reward_key] = torch.ones(*nested_batch_size, 1)
        td["next", done_key] = torch.ones(*nested_batch_size, 1)
        td["next", obs_key] = torch.ones(*nested_batch_size, 1)

        input_td = td

        td = step_mdp(
            td.lock_(),
            exclude_reward=exclude_reward,
            exclude_done=exclude_done,
            exclude_action=exclude_action,
            reward_key=reward_key,
            done_key=done_key,
            action_key=action_key,
            keep_other=keep_other,
        )
        td_nested_keys = td.keys(True, True)
        td_keys = td.keys()

        assert td.batch_size == input_td.batch_size
        # Obs will always be present
        assert obs_key in td_nested_keys
        # Nested key should not be present in this specific conditions
        if (
            (exclude_done or not nested_done)
            and (exclude_reward or not nested_reward)
            and (exclude_action or not nested_action)
            and not nested_obs
            and ((not keep_other) or (keep_other and not nested_other))
        ):
            assert nested_key[0] not in td_keys
        else:  # Nested key is present
            assert not td[nested_key] is input_td["next", nested_key]
            assert not td[nested_key] is input_td[nested_key]
            assert td[nested_key].batch_size == nested_batch_size
        # If we exclude everything we are left with just obs
        if exclude_done and exclude_reward and exclude_action and not keep_other:
            if nested_obs:
                assert len(td_nested_keys) == 1 and list(td_nested_keys)[0] == obs_key
            else:
                assert len(td_nested_keys) == 1 and list(td_nested_keys)[0] == obs_key
        # Key-wise exclusions
        if not exclude_reward:
            assert reward_key in td_nested_keys
            assert (td[reward_key] == 1).all()
        else:
            assert reward_key not in td_nested_keys
        if not exclude_action:
            assert action_key in td_nested_keys
            assert (td[action_key] == 0).all()
        else:
            assert action_key not in td_nested_keys
        if not exclude_done:
            assert done_key in td_nested_keys
            assert (td[done_key] == 1).all()
        else:
            assert done_key not in td_nested_keys
        if keep_other:
            assert other_key in td_nested_keys
            assert (td[other_key] == 0).all()
        else:
            assert other_key not in td_nested_keys

    @pytest.mark.parametrize("nested_other", [True, False])
    @pytest.mark.parametrize("exclude_reward", [True, False])
    @pytest.mark.parametrize("exclude_done", [True, False])
    @pytest.mark.parametrize("exclude_action", [True, False])
    @pytest.mark.parametrize("keep_other", [True, False])
    def test_nested_partially(
        self,
        nested_other,
        exclude_reward,
        exclude_done,
        exclude_action,
        keep_other,
    ):
        # General
        td_batch_size = (4,)
        nested_batch_size = (4, 3)
        nested_key = ("data",)
        reward_key = "reward"
        done_key = "done"
        action_key = "action"
        obs_key = "state"
        other_key = "beatles"
        if nested_other:
            other_key = nested_key + (other_key,)

        # Nested only in root
        td = TensorDict(
            {
                nested_key: TensorDict({}, nested_batch_size),
                "next": {},
            },
            td_batch_size,
        )

        td[reward_key] = torch.zeros(*nested_batch_size, 1)
        td[done_key] = torch.zeros(*nested_batch_size, 1)
        td[obs_key] = torch.zeros(*nested_batch_size, 1)
        td[action_key] = torch.zeros(*nested_batch_size, 1)
        td[other_key] = torch.zeros(*nested_batch_size, 1)

        td["next", reward_key] = torch.zeros(*nested_batch_size, 1)
        td["next", done_key] = torch.zeros(*nested_batch_size, 1)
        td["next", obs_key] = torch.zeros(*nested_batch_size, 1)

        td = step_mdp(
            td.lock_(),
            exclude_reward=exclude_reward,
            exclude_done=exclude_done,
            exclude_action=exclude_action,
            reward_key=reward_key,
            done_key=done_key,
            action_key=action_key,
            keep_other=keep_other,
        )
        td_keys_nested = td.keys(True, True)
        td_keys = td.keys()
        if keep_other:
            if nested_other:
                assert nested_key[0] in td_keys
                assert td[nested_key].batch_size == nested_batch_size
            else:
                assert nested_key[0] not in td_keys
            assert (td[other_key] == 0).all()
        else:
            assert other_key not in td_keys_nested

        # Nested only in next
        td = TensorDict(
            {
                "next": {nested_key: TensorDict({}, nested_batch_size)},
            },
            td_batch_size,
        )
        td[reward_key] = torch.zeros(*nested_batch_size, 1)
        td[done_key] = torch.zeros(*nested_batch_size, 1)
        td[obs_key] = torch.zeros(*nested_batch_size, 1)
        td[action_key] = torch.zeros(*nested_batch_size, 1)

        td["next", other_key] = torch.zeros(*nested_batch_size, 1)
        td["next", reward_key] = torch.zeros(*nested_batch_size, 1)
        td["next", done_key] = torch.zeros(*nested_batch_size, 1)
        td["next", obs_key] = torch.zeros(*nested_batch_size, 1)

        td = step_mdp(
            td.lock_(),
            exclude_reward=exclude_reward,
            exclude_done=exclude_done,
            exclude_action=exclude_action,
            reward_key=reward_key,
            done_key=done_key,
            action_key=action_key,
            keep_other=keep_other,
        )
        td_keys = td.keys()

        if nested_other:
            assert nested_key[0] in td_keys
            assert td[nested_key].batch_size == nested_batch_size
        else:
            assert nested_key[0] not in td_keys
        assert (td[other_key] == 0).all()


@pytest.mark.parametrize("device", get_default_devices())
def test_batch_locked(device):
    env = MockBatchedLockedEnv(device)
    assert env.batch_locked

    with pytest.raises(RuntimeError, match="batch_locked is a read-only property"):
        env.batch_locked = False
    td = env.reset()
    td["action"] = env.action_spec.rand()
    td_expanded = td.expand(2).clone()
    _ = env.step(td)

    with pytest.raises(
        RuntimeError, match="Expected a tensordict with shape==env.shape, "
    ):
        env.step(td_expanded)


@pytest.mark.parametrize("device", get_default_devices())
def test_batch_unlocked(device):
    env = MockBatchedUnLockedEnv(device)
    assert not env.batch_locked

    with pytest.raises(RuntimeError, match="batch_locked is a read-only property"):
        env.batch_locked = False
    td = env.reset()
    td["action"] = env.action_spec.rand()
    td_expanded = td.expand(2).clone()
    td = env.step(td)

    env.step(td_expanded)


@pytest.mark.parametrize("device", get_default_devices())
def test_batch_unlocked_with_batch_size(device):
    env = MockBatchedUnLockedEnv(device, batch_size=torch.Size([2]))
    assert not env.batch_locked

    with pytest.raises(RuntimeError, match="batch_locked is a read-only property"):
        env.batch_locked = False

    td = env.reset()
    td["action"] = env.action_spec.rand()
    td_expanded = td.expand(2, 2).reshape(-1).to_tensordict()
    td = env.step(td)

    with pytest.raises(
        RuntimeError, match="Expected a tensordict with shape==env.shape, "
    ):
        env.step(td_expanded)


@pytest.mark.skipif(not _has_gym, reason="no gym")
@pytest.mark.skipif(
    gym_version is None or gym_version < version.parse("0.20.0"),
    reason="older versions of half-cheetah do not have 'x_position' info key.",
)
@pytest.mark.parametrize("device", get_default_devices())
def test_info_dict_reader(device, seed=0):
    try:
        import gymnasium as gym
    except ModuleNotFoundError:
        import gym

    env = GymWrapper(gym.make(HALFCHEETAH_VERSIONED), device=device)
    env.set_info_dict_reader(default_info_dict_reader(["x_position"]))

    assert "x_position" in env.observation_spec.keys()
    assert isinstance(env.observation_spec["x_position"], UnboundedContinuousTensorSpec)

    tensordict = env.reset()
    tensordict = env.rand_step(tensordict)

    assert env.observation_spec["x_position"].is_in(tensordict[("next", "x_position")])

    env2 = GymWrapper(gym.make("HalfCheetah-v4"))
    env2.set_info_dict_reader(
        default_info_dict_reader(
            ["x_position"], spec={"x_position": OneHotDiscreteTensorSpec(5)}
        )
    )

    tensordict2 = env2.reset()
    tensordict2 = env2.rand_step(tensordict2)

    assert not env2.observation_spec["x_position"].is_in(
        tensordict2[("next", "x_position")]
    )


def test_make_spec_from_td():
    data = TensorDict(
        {
            "obs": torch.randn(3),
            "action": torch.zeros(2, dtype=torch.int),
            "next": {
                "obs": torch.randn(3),
                "reward": torch.randn(1),
                "done": torch.zeros(1, dtype=torch.bool),
            },
        },
        [],
    )
    spec = make_composite_from_td(data)
    assert (spec.zero() == data.zero_()).all()
    for key, val in data.items(True, True):
        assert val.dtype is spec[key].dtype


@pytest.mark.skipif(not torch.cuda.device_count(), reason="No cuda device")
class TestConcurrentEnvs:
    """Concurrent parallel envs on multiple procs can interfere."""

    class Policy(TensorDictModuleBase):
        in_keys = []
        out_keys = ["action"]

        def __init__(self, spec):
            super().__init__()
            self.spec = spec

        def forward(self, tensordict):
            tensordict.set("action", self.spec["action"].zero() + 1)
            return tensordict

    @staticmethod
    def main_penv(j, q=None):
        device = "cpu" if not torch.cuda.device_count() else "cuda:0"
        n_workers = 1
        env_p = ParallelEnv(
            n_workers,
            [
                lambda i=i: CountingEnv(i, device=device)
                for i in range(j, j + n_workers)
            ],
        )
        env_s = SerialEnv(
            n_workers,
            [
                lambda i=i: CountingEnv(i, device=device)
                for i in range(j, j + n_workers)
            ],
        )
        spec = env_p.action_spec
        policy = TestConcurrentEnvs.Policy(CompositeSpec(action=spec.to(device)))
        N = 10
        r_p = []
        r_s = []
        for _ in range(N):
            with torch.no_grad():
                r_p.append(env_s.rollout(100, break_when_any_done=False, policy=policy))
                r_s.append(env_p.rollout(100, break_when_any_done=False, policy=policy))

        td_equals = torch.stack(r_p).contiguous() == torch.stack(r_s).contiguous()
        if td_equals.all():
            if q is not None:
                q.put(("passed", j))
            else:
                pass
        else:
            if q is not None:
                s = ""
                for key, item in td_equals.items(True, True):
                    if not item.all():
                        s = s + f"\t{key}"
                q.put((f"failed: {s}", j))
            else:
                raise RuntimeError()

    @staticmethod
    def main_collector(j, q=None):
        device = "cpu" if not torch.cuda.device_count() else "cuda:0"
        N = 10
        n_workers = 1
        make_envs = [
            lambda i=i: CountingEnv(i, device=device) for i in range(j, j + n_workers)
        ]
        spec = make_envs[0]().action_spec
        policy = TestConcurrentEnvs.Policy(CompositeSpec(action=spec))
        collector = MultiSyncDataCollector(
            make_envs,
            policy,
            frames_per_batch=n_workers * 100,
            total_frames=N * n_workers * 100,
        )
        single_collectors = [
            SyncDataCollector(
                make_envs[i](),
                policy,
                frames_per_batch=n_workers * 100,
                total_frames=N * n_workers * 100,
            )
            for i in range(n_workers)
        ]
        collector = iter(collector)
        single_collectors = [iter(sc) for sc in single_collectors]

        r_p = []
        r_s = []
        for _ in range(N):
            with torch.no_grad():
                r_p.append(next(collector).clone())
                r_s.append(torch.cat([next(sc) for sc in single_collectors]))

        r_p = torch.stack(r_p).contiguous()
        r_s = torch.stack(r_s).contiguous()
        td_equals = r_p == r_s

        if td_equals.all():
            if q is not None:
                q.put(("passed", j))
            else:
                pass
        else:
            if q is not None:
                s = ""
                for key, item in td_equals.items(True, True):
                    if not item.all():
                        print(key, "failed")
                        print("r_p", r_p.get(key)[~item])
                        print("r_s", r_s.get(key)[~item])
                        s = s + f"\t{key}"
                q.put((f"failed: {s}", j))
            else:
                raise RuntimeError()

    @pytest.mark.parametrize("nproc", [3, 1])
    def test_mp_concurrent(self, nproc):
        if nproc == 1:
            self.main_penv(3)
            self.main_penv(6)
            self.main_penv(9)
        else:
            from torch import multiprocessing as mp

            q = mp.Queue(3)
            ps = []
            try:
                for k in range(3, 10, 3):
                    p = mp.Process(target=type(self).main_penv, args=(k, q))
                    ps.append(p)
                    p.start()
                for _ in range(3):
                    msg, j = q.get(timeout=100)
                    assert msg == "passed", j
            finally:
                for p in ps:
                    p.join()

    @pytest.mark.parametrize("nproc", [3, 1])
    def test_mp_collector(self, nproc):
        if nproc == 1:
            self.main_collector(3)
            self.main_collector(6)
            self.main_collector(9)
        else:
            from torch import multiprocessing as mp

            q = mp.Queue(3)
            ps = []
            try:
                for j in range(3, 10, 3):
                    p = mp.Process(target=type(self).main_collector, args=(j, q))
                    ps.append(p)
                    p.start()
                for _ in range(3):
                    msg, j = q.get(timeout=100)
                    assert msg == "passed", j
            finally:
                for p in ps:
                    p.join(timeout=2)


class TestNestedSpecs:
    @pytest.mark.parametrize("envclass", ["CountingEnv", "NestedCountingEnv"])
    def test_nested_env(self, envclass):

        if envclass == "CountingEnv":
            env = CountingEnv()
        elif envclass == "NestedCountingEnv":
            env = NestedCountingEnv()
        else:
            raise NotImplementedError
        reset = env.reset()
        assert not isinstance(env.done_spec, CompositeSpec)
        assert not isinstance(env.reward_spec, CompositeSpec)
        assert env.done_spec == env.output_spec[("_done_spec", *env.done_key)]
        assert env.reward_spec == env.output_spec[("_reward_spec", *env.reward_key)]
        if envclass == "NestedCountingEnv":
            assert env.done_key == ("data", "done")
            assert env.reward_key == ("data", "reward")
            assert ("data", "done") in reset.keys(True)
            assert ("data", "states") in reset.keys(True)
            assert ("data", "reward") not in reset.keys(True)
        assert env.done_key in reset.keys(True)
        assert env.reward_key not in reset.keys(True)

        next_state = env.rand_step()
        if envclass == "NestedCountingEnv":
            assert ("next", "data", "done") in next_state.keys(True)
            assert ("next", "data", "states") in next_state.keys(True)
            assert ("next", "data", "reward") in next_state.keys(True)
        assert ("next", *env.done_key) in next_state.keys(True)
        assert ("next", *env.reward_key) in next_state.keys(True)

    @pytest.mark.parametrize("batch_size", [(), (32,), (32, 1)])
    def test_nested_env_dims(self, batch_size, nested_dim=5, rollout_length=3):

        env = NestedCountingEnv(batch_size=batch_size, nested_dim=nested_dim)

        td_reset = env.reset()
        assert td_reset.batch_size == batch_size
        assert td_reset["data"].batch_size == (*batch_size, nested_dim)

        td = env.rand_action()
        assert td.batch_size == batch_size
        assert td["data"].batch_size == (*batch_size, nested_dim)

        td = env.rand_action(td_reset)
        assert td.batch_size == batch_size
        assert td["data"].batch_size == (*batch_size, nested_dim)

        td = env.rand_step(td)
        assert td.batch_size == batch_size
        assert td["data"].batch_size == (*batch_size, nested_dim)
        assert td["next", "data"].batch_size == (*batch_size, nested_dim)

        td = env.rand_step()
        assert td.batch_size == batch_size
        assert td["data"].batch_size == (*batch_size, nested_dim)
        assert td["next", "data"].batch_size == (*batch_size, nested_dim)

        td = env.rand_step(td_reset)
        assert td.batch_size == batch_size
        assert td["data"].batch_size == (*batch_size, nested_dim)
        assert td["next", "data"].batch_size == (*batch_size, nested_dim)

        td = env.rollout(rollout_length)
        assert td.batch_size == (*batch_size, rollout_length)
        assert td["data"].batch_size == (*batch_size, rollout_length, nested_dim)
        assert td["next", "data"].batch_size == (
            *batch_size,
            rollout_length,
            nested_dim,
        )

        policy = CountingEnvCountPolicy(env.action_spec, env.action_key)
        td = env.rollout(rollout_length, policy)
        assert td.batch_size == (*batch_size, rollout_length)
        assert td["data"].batch_size == (*batch_size, rollout_length, nested_dim)
        assert td["next", "data"].batch_size == (
            *batch_size,
            rollout_length,
            nested_dim,
        )


@pytest.mark.parametrize(
    "envclass",
    [
        ContinuousActionConvMockEnv,
        ContinuousActionConvMockEnvNumpy,
        ContinuousActionVecMockEnv,
        CountingBatchedEnv,
        CountingEnv,
        DiscreteActionConvMockEnv,
        DiscreteActionConvMockEnvNumpy,
        DiscreteActionVecMockEnv,
        partial(
            DummyModelBasedEnvBase, world_model=TestModelBasedEnvBase.world_model()
        ),
        MockBatchedLockedEnv,
        MockBatchedUnLockedEnv,
        MockSerialEnv,
        NestedCountingEnv,
    ],
)
def test_mocking_envs(envclass):
    env = envclass()
    env.set_seed(100)
    reset = env.reset()
    _ = env.rand_step(reset)
    check_env_specs(env, seed=100)


if __name__ == "__main__":
    args, unknown = argparse.ArgumentParser().parse_known_args()
    pytest.main([__file__, "--capture", "no", "--exitfirst"] + unknown)
