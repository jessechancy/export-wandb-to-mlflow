import re
from functools import partial

from mlflow.entities import Metric

from export_wandb_to_mlflow.config import MLFLOW_MAXIMUM_METRICS_PER_BATCH


def _convert_bytes_to_mb(row, key):
    return None if row.get(key, None) is None else round(row[key] / 1000000.0, 2)


def _convert_gb_to_mb(row, key):
    return None if row.get(key, None) is None else round(row[key] * 1000.0, 2)


_GPU_METRICS_MAPPING = {
    "system.gpu.(\d+).memory$": "system/gpu_{i}_utilization_percentage",
    "system.gpu.(\d+).memoryAllocated$": "system/gpu_{i}_memory_usage_percentage",
    "system.gpu.(\d+).memoryAllocatedBytes": "system/gpu_{i}_memory_usage_megabytes",
    "system.gpu.(\d+).powerWatts": "system/gpu_{i}_power_watts",
    "system.gpu.(\d+).powerPercent": "system/gpu_{i}_power_percentage",
}

_SYSTEM_METRICS_MAPPING = {
    "system/cpu_utilization_percentage": "system.cpu",
    "system/disk_usage_megabytes": partial(_convert_gb_to_mb, key="system.disk.\\.usageGB"),
    "system/disk_usage_percentage": "system.disk.\\.usagePercent",
    "system/system_memory_usage_megabytes": "system.proc.memory.rssMB",
    "system/system_memory_usage_percentage": "system.memory",
    "system/network_receive_megabytes": partial(_convert_bytes_to_mb, key="system.network.recv"),
    "system/network_transmit_megabytes": partial(_convert_bytes_to_mb, key="system.network.sent"),
}


def _convert_gpu_metrics_to_mlflow(row, step):
    metrics = []

    for k, v in row.items():
        for wandb_key, mlflow_key in _GPU_METRICS_MAPPING.items():
            # Check if the current metrics matches the GPU metrics pattern.
            match_result = re.search(wandb_key, k)

            if match_result and v is not None:
                gpu_index = match_result.group(1)
                mlflow_key = mlflow_key.format(i=gpu_index)
                if "memoryAllocatedBytes" in wandb_key:
                    # Convert bytes to MB for metric `memoryAllocatedBytes`.
                    memory_usage_mb = round(v / 1e6, 2)
                    # Wandb does not provide timestamp for system metrics, we use step as a
                    # workaround.
                    metrics.append(Metric(mlflow_key, memory_usage_mb, timestamp=step, step=step))
                else:
                    metrics.append(Metric(mlflow_key, v, timestamp=step, step=step))
    return metrics


def convert_wandb_system_metrics_to_mlflow(wandb_run, mlflow_client, mlflow_run_id):
    """Convert Wandb system metrics to MLflow.

    This function converts Wandb system metrics for the given `wandb_run` to MLflow system metrics,
    and log to the MLflow run with ID `mlflow_run_id`. All logging happens asynchronously.

    Args:
        wandb_run (wandb.sdk.wandb_run.Run): The Wandb run object.
        mlflow_client (mlflow.client.MlflowClient): The MLflow client.
        mlflow_run_id (str): The MLflow run ID.
    """
    wandb_system_metrics = wandb_run.history(stream="system")

    mlflow_system_metrics = []

    for index, row in wandb_system_metrics.iterrows():
        gpu_metrics = _convert_gpu_metrics_to_mlflow(row, step=index)
        non_gpu_metrics = []
        for mlflow_key, wandb_handler in _SYSTEM_METRICS_MAPPING.items():
            val = wandb_handler(row) if callable(wandb_handler) else row.get(wandb_handler, None)

            if val:
                # Some system metrics in a row could be None, which are not logged to MLflow.
                non_gpu_metrics.append(Metric(mlflow_key, val, index, index))

        metrics_count = len(mlflow_system_metrics) + len(gpu_metrics) + len(non_gpu_metrics)
        if metrics_count >= MLFLOW_MAXIMUM_METRICS_PER_BATCH:
            # Trigger logging when we reach the maximum metrics allowed per batch.
            mlflow_client.log_batch(mlflow_run_id, metrics=mlflow_system_metrics, synchronous=False)
            mlflow_system_metrics = gpu_metrics + non_gpu_metrics
        else:
            mlflow_system_metrics.extend(gpu_metrics + non_gpu_metrics)

    # Clear up leftovers.
    mlflow_client.log_batch(mlflow_run_id, metrics=mlflow_system_metrics, synchronous=False)
