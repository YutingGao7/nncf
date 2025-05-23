# Copyright (c) 2024 Intel Corporation
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#      http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import json
import subprocess
import sys
from abc import ABC
from abc import abstractmethod
from copy import deepcopy
from pathlib import Path
from typing import Callable, Dict, List, Set, TypeVar, Union

import numpy as np

import nncf
from nncf.common.utils.os import is_linux
from nncf.common.utils.os import is_windows
from tests.shared.paths import GITHUB_REPO_URL
from tests.shared.paths import PROJECT_ROOT

TensorType = TypeVar("TensorType")


def get_cli_dict_args(args):
    cli_args = {}
    for key, val in args.items():
        cli_key = "--{}".format(str(key))
        cli_args[cli_key] = None
        if val is not None:
            cli_args[cli_key] = str(val)
    return cli_args


def create_venv_with_nncf(tmp_path: Path, package_type: str, venv_type: str, extra_reqs: Set[str] = None):
    venv_path = tmp_path / "venv"
    venv_path.mkdir()

    python_executable_with_venv = get_python_executable_with_venv(venv_path)
    pip_with_venv = get_pip_executable_with_venv(venv_path)

    version_string = f"{sys.version_info[0]}.{sys.version_info[1]}"

    if venv_type == "virtualenv":
        virtualenv = Path(sys.executable).parent / "virtualenv"
        subprocess.check_call(f"{virtualenv} -ppython{version_string} {venv_path}", shell=True)
    elif venv_type == "venv":
        subprocess.check_call(f"{sys.executable} -m venv {venv_path}", shell=True)

    subprocess.check_call(f"{pip_with_venv} install --upgrade pip", shell=True)
    subprocess.check_call(f"{pip_with_venv} install --upgrade wheel setuptools", shell=True)

    if package_type in ["build_s", "build_w"]:
        subprocess.check_call(f"{pip_with_venv} install build", shell=True)

    run_path = tmp_path / "run"
    run_path.mkdir()
    extra_reqs_str = ""
    if extra_reqs is not None and extra_reqs:
        extra_reqs_str = ",".join(extra_reqs)
        extra_reqs_str = f"[{extra_reqs_str}]"

    if package_type == "pip_pypi":
        run_cmd_line = f"{pip_with_venv} install nncf{extra_reqs_str}"
    elif package_type == "pip_local":
        run_cmd_line = f"{pip_with_venv} install {PROJECT_ROOT}{extra_reqs_str}"
    elif package_type == "pip_e_local":
        run_cmd_line = f"{pip_with_venv} install -e {PROJECT_ROOT}{extra_reqs_str}"
    elif package_type == "pip_git_develop":
        run_cmd_line = f"{pip_with_venv} install git+{GITHUB_REPO_URL}@develop#egg=nncf{extra_reqs_str}"
    elif package_type == "build_s":
        run_cmd_line = f"{python_executable_with_venv} -m build -n -s"
    elif package_type == "build_w":
        run_cmd_line = f"{python_executable_with_venv} -m build -n -w"
    else:
        raise nncf.ValidationError(f"Invalid package type: {package_type}")

    # Currently CI runs on RTX3090s, which require CUDA 11 to work.
    # Current torch, however (v1.12), is installed via pip using .whl packages
    # compiled for CUDA 10.2. Thus need to direct pip installation specifically for
    # torch, otherwise the NNCF will only work in CPU mode.
    torch_extra_index = " --extra-index-url https://download.pytorch.org/whl/cu116"
    if extra_reqs is not None and "torch" in extra_reqs and "build" not in package_type:
        run_cmd_line += torch_extra_index

    subprocess.run(run_cmd_line, check=True, shell=True, cwd=PROJECT_ROOT)
    return venv_path


class BaseTensorListComparator(ABC):
    @classmethod
    @abstractmethod
    def _to_numpy(cls, tensor: TensorType) -> np.ndarray:
        pass

    @classmethod
    def _check_assertion(
        cls,
        test: Union[TensorType, List[TensorType]],
        reference: Union[TensorType, List[TensorType]],
        assert_fn: Callable[[np.ndarray, np.ndarray], bool],
    ):
        if not isinstance(test, list):
            test = [test]
        if not isinstance(reference, list):
            reference = [reference]
        assert len(test) == len(reference)

        for x, y in zip(test, reference):
            x = cls._to_numpy(x)
            y = cls._to_numpy(y)
            assert_fn(x, y)

    @classmethod
    def check_equal(
        cls,
        test: Union[TensorType, List[TensorType]],
        reference: Union[TensorType, List[TensorType]],
        rtol: float = 1e-1,
        atol=0,
    ):
        cls._check_assertion(test, reference, lambda x, y: np.testing.assert_allclose(x, y, rtol=rtol, atol=atol))

    @classmethod
    def check_not_equal(
        cls,
        test: Union[TensorType, List[TensorType]],
        reference: Union[TensorType, List[TensorType]],
        rtol: float = 1e-4,
    ):
        cls._check_assertion(
            test,
            reference,
            lambda x, y: np.testing.assert_raises(AssertionError, np.testing.assert_allclose, x, y, rtol=rtol),
        )

    @classmethod
    def check_less(
        cls, test: Union[TensorType, List[TensorType]], reference: Union[TensorType, List[TensorType]], rtol=1e-4
    ):
        cls.check_not_equal(test, reference, rtol=rtol)
        cls._check_assertion(test, reference, np.testing.assert_array_less)

    @classmethod
    def check_greater(
        cls, test: Union[TensorType, List[TensorType]], reference: Union[TensorType, List[TensorType]], rtol=1e-4
    ):
        cls.check_not_equal(test, reference, rtol=rtol)
        cls._check_assertion(
            test, reference, lambda x, y: np.testing.assert_raises(AssertionError, np.testing.assert_array_less, x, y)
        )


def telemetry_send_event_test_driver(mocker, use_nncf_fn: Callable):
    from nncf.telemetry import telemetry

    telemetry_send_event_spy = mocker.spy(telemetry, "send_event")
    use_nncf_fn()
    telemetry_send_event_spy.assert_called()


def load_json(stats_path: Path):
    with open(stats_path, "r", encoding="utf8") as json_file:
        return json.load(json_file)


class NumpyEncoder(json.JSONEncoder):
    """Special json encoder for numpy types"""

    def default(self, o):
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.floating):
            return float(o)
        if isinstance(o, np.ndarray):
            return o.tolist()
        return json.JSONEncoder.default(self, o)


def dump_to_json(local_path: Path, data: Dict[str, np.ndarray]):
    with open(local_path, "w", encoding="utf8") as file:
        json.dump(deepcopy(data), file, indent=4, cls=NumpyEncoder)


def compare_stats(expected: Dict[str, np.ndarray], actual: Dict[str, np.ndarray]):
    assert len(expected) == len(actual)
    for ref_node_name, ref_stats in expected.items():
        actual_stats = actual[ref_node_name]
        for param_name, ref_param in ref_stats.items():
            actual_param = actual_stats.get(param_name)
        assert np.array(ref_param).shape == np.array(actual_param).shape
        assert np.allclose(ref_param, actual_param, atol=1e-5)


def get_python_executable_with_venv(venv_path: Path) -> str:
    if is_linux():
        python_executable_with_venv = f". {venv_path}/bin/activate && {venv_path}/bin/python"
    elif is_windows():
        python_executable_with_venv = f" {venv_path}\\Scripts\\activate && python"

    return python_executable_with_venv


def get_pip_executable_with_venv(venv_path: Path) -> str:
    if is_linux():
        pip_with_venv = f". {venv_path}/bin/activate && {venv_path}/bin/pip"
    elif is_windows():
        pip_with_venv = f" {venv_path}\\Scripts\\activate && python -m pip"
    return pip_with_venv


def remove_line_breaks(s: str) -> str:
    return s.replace("\r\n", "").replace("\n", "")
