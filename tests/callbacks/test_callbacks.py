# Copyright 2022 MosaicML Composer authors
# SPDX-License-Identifier: Apache-2.0

import os
import pathlib
from typing import Type, cast

import pytest

from composer.core import Callback, Engine, Event, State
from composer.core.time import Time
from composer.loggers import Logger, LoggerDestination
from composer.loggers.mlflow_logger import MLFlowLogger
from composer.loggers.wandb_logger import WandBLogger
from composer.profiler import Profiler, ProfilerAction
from composer.trainer import Trainer
from tests.callbacks.callback_settings import get_cb_kwargs, get_cb_model_and_datasets, get_cbs_and_marks
from tests.common import EventCounterCallback


def test_callbacks_map_to_events():
    # callback methods must be 1:1 mapping with events
    # exception for private methods
    cb = Callback()
    excluded_methods = ['state_dict', 'load_state_dict', 'run_event', 'close', 'post_close']
    methods = {m for m in dir(cb) if (m not in excluded_methods and not m.startswith('_'))}
    event_names = {e.value for e in Event}
    assert methods == event_names


@pytest.mark.parametrize('event', list(Event))
def test_run_event_callbacks(event: Event, dummy_state: State):
    callback = EventCounterCallback()
    logger = Logger(dummy_state)
    dummy_state.callbacks = [callback]
    engine = Engine(state=dummy_state, logger=logger)

    engine.run_event(event)

    assert callback.event_to_num_calls[event] == 1


@pytest.mark.parametrize('cb_cls', get_cbs_and_marks(callbacks=True, loggers=True, profilers=True))
class TestCallbacks:

    @classmethod
    def setup_class(cls):
        pytest.importorskip('wandb', reason='WandB is optional.')

    def test_callback_is_constructable(self, cb_cls: Type[Callback]):
        cb_kwargs = get_cb_kwargs(cb_cls)
        cb = cb_cls(**cb_kwargs)
        assert isinstance(cb_cls, type)
        assert isinstance(cb, cb_cls)

    def test_multiple_fit_start_and_end(self, cb_cls: Type[Callback], dummy_state: State, tmp_path: pathlib.Path):
        """Test that callbacks do not crash when Event.FIT_START and Event.FIT_END is called multiple times."""
        cb_kwargs = get_cb_kwargs(cb_cls)
        if issubclass(cb_cls, MLFlowLogger):
            cb_kwargs['tracking_uri'] = tmp_path
        if issubclass(cb_cls, WandBLogger):
            os.environ['WANDB_DISABLE_GIT'] = 'true'
            os.environ['WANDB_PROGRAM'] = ''
            os.environ['WANDB_DISABLE_CODE'] = 'true'
        dummy_state.callbacks.append(cb_cls(**cb_kwargs))
        dummy_state.profiler = Profiler(schedule=lambda _: ProfilerAction.SKIP, trace_handlers=[])
        dummy_state.profiler.bind_to_state(dummy_state)

        logger = Logger(dummy_state)
        engine = Engine(state=dummy_state, logger=logger)

        engine.run_event(Event.INIT)  # always runs just once per engine

        engine.run_event(Event.FIT_START)
        engine.run_event(Event.FIT_END)

        engine.run_event(Event.FIT_START)
        engine.run_event(Event.FIT_END)

    def test_idempotent_close(self, cb_cls: Type[Callback], dummy_state: State, tmp_path: pathlib.Path):
        """Test that callbacks do not crash when .close() and .post_close() are called multiple times."""
        cb_kwargs = get_cb_kwargs(cb_cls)
        if issubclass(cb_cls, MLFlowLogger):
            cb_kwargs['tracking_uri'] = tmp_path
        if issubclass(cb_cls, WandBLogger):
            os.environ['WANDB_DISABLE_GIT'] = 'true'
            os.environ['WANDB_PROGRAM'] = ''
            os.environ['WANDB_DISABLE_CODE'] = 'true'
        dummy_state.callbacks.append(cb_cls(**cb_kwargs))
        dummy_state.profiler = Profiler(schedule=lambda _: ProfilerAction.SKIP, trace_handlers=[])
        dummy_state.profiler.bind_to_state(dummy_state)

        logger = Logger(dummy_state)
        engine = Engine(state=dummy_state, logger=logger)

        engine.run_event(Event.INIT)  # always runs just once per engine
        engine.close()
        engine.close()

    def test_multiple_init_and_close(self, cb_cls: Type[Callback], dummy_state: State, tmp_path: pathlib.Path):
        """Test that callbacks do not crash when INIT/.close()/.post_close() are called multiple times in that order."""
        cb_kwargs = get_cb_kwargs(cb_cls)
        if issubclass(cb_cls, MLFlowLogger):
            cb_kwargs['tracking_uri'] = tmp_path
        dummy_state.callbacks.append(cb_cls(**cb_kwargs))
        dummy_state.profiler = Profiler(schedule=lambda _: ProfilerAction.SKIP, trace_handlers=[])
        dummy_state.profiler.bind_to_state(dummy_state)

        logger = Logger(dummy_state)
        engine = Engine(state=dummy_state, logger=logger)

        engine.run_event(Event.INIT)
        engine.close()
        # For good measure, also test idempotent close, in case if there are edge cases with a second call to INIT
        engine.close()

        # Create a new engine, since the engine does allow events to run after it has been closed
        engine = Engine(state=dummy_state, logger=logger)
        engine.close()
        # For good measure, also test idempotent close, in case if there are edge cases with a second call to INIT
        engine.close()


@pytest.mark.parametrize('cb_cls', get_cbs_and_marks(callbacks=True, loggers=True, profilers=True))
# Parameterized across @pytest.mark.remote as some loggers (e.g. wandb) support integration testing
@pytest.mark.parametrize('device_train_microbatch_size,_remote',
                         [(1, False),
                          (2, False), pytest.param(1, True, marks=pytest.mark.remote)])
@pytest.mark.filterwarnings(r'ignore:The profiler is enabled:UserWarning')
class TestCallbackTrains:

    def _get_trainer(self, cb: Callback, device_train_microbatch_size: int):
        loggers = cb if isinstance(cb, LoggerDestination) else None
        callbacks = cb if not isinstance(cb, LoggerDestination) else None

        model, train_dataloader, eval_dataloader = get_cb_model_and_datasets(cb, dl_size=4, batch_size=2)

        return Trainer(
            model=model,
            train_dataloader=train_dataloader,
            eval_dataloader=eval_dataloader,
            max_duration=2,
            device_train_microbatch_size=device_train_microbatch_size,
            callbacks=callbacks,
            loggers=loggers,
            profiler=Profiler(schedule=lambda _: ProfilerAction.SKIP, trace_handlers=[]),
        )

    def test_trains(self, cb_cls: Type[Callback], device_train_microbatch_size: int, _remote: bool, tmp_path: pathlib.Path):
        del _remote  # unused. `_remote` must be passed through to parameterize the test markers.
        cb_kwargs = get_cb_kwargs(cb_cls)
        if issubclass(cb_cls, MLFlowLogger):
            cb_kwargs['tracking_uri'] = tmp_path
        if issubclass(cb_cls, WandBLogger):
            os.environ['WANDB_DISABLE_GIT'] = 'true'
            os.environ['WANDB_PROGRAM'] = ''
            os.environ['WANDB_DISABLE_CODE'] = 'true'
        cb = cb_cls(**cb_kwargs)
        trainer = self._get_trainer(cb, device_train_microbatch_size)
        trainer.fit()

    def test_trains_multiple_calls(self, cb_cls: Type[Callback], device_train_microbatch_size: int, _remote: bool, tmp_path: pathlib.Path):
        """
        Tests that training with multiple fits complete.
        Note: future functional tests should test for
        idempotency (e.g functionally)
        """
        del _remote  # unused. `_remote` must be passed through to parameterize the test markers.
        cb_kwargs = get_cb_kwargs(cb_cls)
        cb = cb_cls(**cb_kwargs)
        if issubclass(cb_cls, MLFlowLogger):
            cb_kwargs['tracking_uri'] = tmp_path
        if issubclass(cb_cls, WandBLogger):
            os.environ['WANDB_DISABLE_GIT'] = 'true'
            os.environ['WANDB_PROGRAM'] = ''
            os.environ['WANDB_DISABLE_CODE'] = 'true'
        trainer = self._get_trainer(cb, device_train_microbatch_size)
        trainer.fit()

        assert trainer.state.max_duration is not None
        trainer.state.max_duration = cast(Time[int], trainer.state.max_duration * 2)

        trainer.fit()
