import copy
import os
import sys
import pytest
import subprocess
import json

import ray
from ray.cluster_utils import AutoscalingCluster
from ray._private.test_utils import wait_for_condition

LONG_STR_VALID = "".join("a" for _ in range(ray._raylet.RAY_LABEL_MAX_LENGTH - 1))
LONG_STR_EXCEEDED = "".join("a" for _ in range(ray._raylet.RAY_LABEL_MAX_LENGTH))
ALL_LETTERS_VALID_STR = "abc.com/1234567890-_.abcdefghijklmnopqrstuvwxyzABC"
VALID_LABELS = {
    "gpu_type": "A100",
    "region": "us",
    ALL_LETTERS_VALID_STR: ALL_LETTERS_VALID_STR,
    LONG_STR_VALID: LONG_STR_VALID,
    "empty_value": "",
}
VALID_LABELS_JSON = json.dumps(VALID_LABELS, separators=(",", ":"))

ILLEGA_KEY_LENGTH_PROMPT = (
    f"cannot be empty or exceed {ray._raylet.RAY_LABEL_MAX_LENGTH - 1} characters"
)
ILLEGA_VALUE_LENGTH_PROMPT = (
    f"cannot be exceed {ray._raylet.RAY_LABEL_MAX_LENGTH - 1} characters"
)
ILLEGA_LETTERS_PROMPT = (
    "consist of letters from `a-z0-9A-Z` or `-` or `_` or `.` or `/`"
)


def check_cmd_stderr(cmd):
    return subprocess.run(cmd, stderr=subprocess.PIPE).stderr.decode("utf-8")


def add_default_labels(node_info, labels):
    final_labels = copy.deepcopy(labels)
    final_labels["ray.io/node_id"] = node_info["NodeID"]
    return final_labels


@pytest.mark.parametrize(
    "call_ray_start",
    [f"ray start --head --labels={VALID_LABELS_JSON}"],
    indirect=True,
)
def test_ray_start_set_node_labels(call_ray_start):
    ray.init(address=call_ray_start)
    node_info = ray.nodes()[0]
    assert node_info["Labels"] == add_default_labels(node_info, VALID_LABELS)


@pytest.mark.parametrize(
    "call_ray_start",
    [
        "ray start --head --labels={}",
    ],
    indirect=True,
)
def test_ray_start_set_empty_node_labels(call_ray_start):
    ray.init(address=call_ray_start)
    node_info = ray.nodes()[0]
    assert node_info["Labels"] == add_default_labels(node_info, {})


def test_ray_init_set_node_labels(shutdown_only):
    ray.init(labels=VALID_LABELS)
    node_info = ray.nodes()[0]
    assert node_info["Labels"] == add_default_labels(node_info, VALID_LABELS)
    ray.shutdown()
    ray.init(labels={})
    node_info = ray.nodes()[0]
    assert node_info["Labels"] == add_default_labels(node_info, {})


def test_ray_init_set_node_labels_value_error(ray_start_cluster):
    cluster = ray_start_cluster

    # cluster.add_node api of node labels.
    key = "ray.io/node_id"
    with pytest.raises(
        ValueError,
        match=f"Custom label keys `{key}` cannot start with the prefix `ray.io/`",
    ):
        cluster.add_node(num_cpus=1, labels={key: "111111"})

    with pytest.raises(
        ValueError,
        match=ILLEGA_KEY_LENGTH_PROMPT,
    ):
        cluster.add_node(num_cpus=1, labels={LONG_STR_EXCEEDED: "111111"})

    with pytest.raises(
        ValueError,
        match=ILLEGA_LETTERS_PROMPT,
    ):
        cluster.add_node(num_cpus=1, labels={"key": "aa%$22"})

    # ray.init api of node labels.
    key = "ray.io/other_key"
    with pytest.raises(
        ValueError,
        match=f"Custom label keys `{key}` cannot start with the prefix `ray.io/`",
    ):
        ray.init(labels={key: "value"})

    with pytest.raises(ValueError, match=ILLEGA_KEY_LENGTH_PROMPT):
        ray.init(labels={LONG_STR_EXCEEDED: "123"})

    with pytest.raises(ValueError, match=ILLEGA_KEY_LENGTH_PROMPT):
        ray.init(labels={"": "123"})

    with pytest.raises(ValueError, match=ILLEGA_VALUE_LENGTH_PROMPT):
        ray.init(labels={"key": LONG_STR_EXCEEDED})

    with pytest.raises(
        ValueError,
        match=ILLEGA_LETTERS_PROMPT,
    ):
        ray.init(labels={"key": "abc@ss"})

    with pytest.raises(
        ValueError,
        match=ILLEGA_LETTERS_PROMPT,
    ):
        ray.init(labels={"k y": "123"})

    cluster.add_node(num_cpus=1)
    with pytest.raises(ValueError, match="labels must not be provided"):
        ray.init(address=cluster.address, labels={"gpu_type": "A100"})

    with pytest.raises(ValueError, match="labels must not be provided"):
        ray.init(labels={"gpu_type": "A100"})


def test_ray_start_set_node_labels_value_error():
    out = check_cmd_stderr(["ray", "start", "--head", "--labels=xxx"])
    assert "is not a valid JSON string, detail error" in out

    out = check_cmd_stderr(["ray", "start", "--head", '--labels={"gpu_type":1}'])
    assert 'The value of the "gpu_type" is not string type' in out

    out = check_cmd_stderr(
        ["ray", "start", "--head", '--labels={"ray.io/node_id":"111"}']
    )
    assert "cannot start with the prefix `ray.io/`" in out

    out = check_cmd_stderr(
        ["ray", "start", "--head", '--labels={"ray.io/other_key":"111"}']
    )
    assert "cannot start with the prefix `ray.io/`" in out

    # Validate illegal length of keys and values.
    out = check_cmd_stderr(
        ["ray", "start", "--head", f'--labels={{"{LONG_STR_EXCEEDED}":"111"}}']
    )
    assert ILLEGA_KEY_LENGTH_PROMPT in out

    out = check_cmd_stderr(
        ["ray", "start", "--head", f'--labels={{"key":"{LONG_STR_EXCEEDED}"}}']
    )
    assert ILLEGA_VALUE_LENGTH_PROMPT in out

    out = check_cmd_stderr(["ray", "start", "--head", '--labels={"":"111"}'])
    assert ILLEGA_KEY_LENGTH_PROMPT in out

    # Validate illegal letters of keys and values.
    out = check_cmd_stderr(["ray", "start", "--head", '--labels={"?$#s":"123"}'])
    assert ILLEGA_LETTERS_PROMPT in out

    out = check_cmd_stderr(["ray", "start", "--head", '--labels={"key":"`1~~"}'])
    assert ILLEGA_LETTERS_PROMPT in out


def test_cluster_add_node_with_labels(ray_start_cluster):
    cluster = ray_start_cluster
    cluster.add_node(num_cpus=1, labels=VALID_LABELS)
    cluster.wait_for_nodes()
    ray.init(address=cluster.address)
    node_info = ray.nodes()[0]
    assert node_info["Labels"] == add_default_labels(node_info, VALID_LABELS)
    head_node_id = ray.nodes()[0]["NodeID"]

    cluster.add_node(num_cpus=1, labels={})
    cluster.wait_for_nodes()
    for node in ray.nodes():
        if node["NodeID"] != head_node_id:
            assert node["Labels"] == add_default_labels(node, {})


def test_autoscaler_set_node_labels(shutdown_only):
    cluster = AutoscalingCluster(
        head_resources={"CPU": 0},
        worker_node_types={
            "worker_1": {
                "resources": {"CPU": 1},
                "labels": {"region": "us"},
                "node_config": {},
                "min_workers": 1,
                "max_workers": 1,
            }
        },
    )

    try:
        cluster.start()
        ray.init()
        wait_for_condition(lambda: len(ray.nodes()) == 2)

        for node in ray.nodes():
            if node["Resources"].get("CPU", 0) == 1:
                assert node["Labels"] == add_default_labels(node, {"region": "us"})
    finally:
        cluster.shutdown()


if __name__ == "__main__":
    if os.environ.get("PARALLEL_CI"):
        sys.exit(pytest.main(["-n", "auto", "--boxed", "-vs", __file__]))
    else:
        sys.exit(pytest.main(["-sv", __file__]))
