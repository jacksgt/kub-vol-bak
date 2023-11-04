#!/usr/bin/env python3

__author__ = "Jack Henschel"
__version__ = "0.1.0"
__license__ = "MIT"

import argparse
from base64 import b64decode, b64encode
import subprocess # see also: https://pypi.org/project/python-shell/
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import os
import json

from typing import List, Dict, Optional

# https://docs.kr8s.org/en/latest/
import kr8s
from kr8s.objects import Pod, Secret, PersistentVolume, PersistentVolumeClaim

DRY_RUN = False
BACKUP_NAMESPACE = "kub-vol-bak"
BACKUP_SECRET_NAME = "backup-credentials"
BACKUP_IMAGE = "docker.io/restic/restic:0.16.0"
VOLUME_BACKUP_TIMEOUT = 3600 # 1h
EXECUTION_ID = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

# Restic integration can only backup volumes that are mounted by a pod and not directly from the PVC. For orphan PVC/PV pairs (without running pods), some Velero users overcame this limitation running a staging pod (i.e. a busybox or alpine container with an infinite sleep) to mount these PVC/PV pairs prior taking a Velero backup.
# https://velero.io/docs/v1.9/restic/

def run_backup_pod(pod_name, node_name, node_path, rbc):
    # TODO: implement resource requests/limits, nice/ionice
    pod = Pod({
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "namespace": BACKUP_NAMESPACE,
            "labels": get_common_labels(),
        },
        "spec": {
            "serviceAccountName": "kub-vol-bak-runner", # TODO: make this configurable
            "containers": [{
                "name": "restic",
                "image": BACKUP_IMAGE,
                "volumeMounts": [
                    {"name": "data", "mountPath": "/data", "readOnly": True},
                    {"name": "tmp", "mountPath": "/tmp", "readOnly": False},
                ],
                "envFrom": [{ "secretRef": {"name": BACKUP_SECRET_NAME }, }, ],
                "env": [
                    # show update messages every 5 minutes,
                    # https://github.com/restic/restic/issues/2706#issuecomment-752182199
                    {"name": "RESTIC_PROGRESS_FPS", "value":"0.0033"},
                ],
                "command": build_restic_cmd(rbc),
                "terminationMessagePolicy": "FallbackToLogsOnError",
            }],
            "volumes": [
                {"name": "data", "hostPath": {"path": node_path, "type": "Directory"}},
                {"name": "tmp", "emptyDir": {}}
            ],
            # "terminationGracePeriodSeconds": 5,
            "restartPolicy": "Never",
            "activeDeadlineSeconds": VOLUME_BACKUP_TIMEOUT,
            "enableServiceLinks": False,
            "nodeName": node_name,
            "automountServiceAccountToken": False,
        },
    })

    if DRY_RUN:
       print(json.dumps(pod.raw))
       return

    # launch pod
    pod.create()

    time.sleep(1)
    pod.wait("condition=Ready")

    for line in pod.logs(follow=True, timeout=None):
        print("> ", line)

    pod.refresh()
    # "2023-11-03T06:17:00Z"
    duration =  datetime.now() - datetime.strptime(pod.status.startTime, '%Y-%m-%dT%H:%M:%SZ')
    print(f"Pod {pod.name} terminated after {pretty_time_delta(duration)}: {pod.status.phase}")

    def cleanup():
       pod.delete()

    return pod, cleanup

def pretty_time_delta(td: timedelta):
    if td.days > 0:
        return f"{td.days}d{td.hours}"
    elif td.hours > 0:
        return f"{td.hours}h{td.minutes}m"
    elif td.minutes > 0:
        return f"{td.minutes}m{td.seconds}s"
    else:
        return f"{td.seconds}s"

# https://docs.python.org/3/library/dataclasses.html
@dataclass
class ResticBackupConfig:
    hostname: str
    # repository: str
    # password: str
    # path: str
    # # node: str
    # pv_name: str
    # hostPath: Optional[str] = None
    exclude_caches: bool = True
    # env_vars: Dict[str,str] = field(default_factory=dict)
    excludes: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str,str] = field(default_factory=dict)

# https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html
def get_backup_config(pvc):
    pv = PersistentVolume.get(pvc.spec.volumeName)

    return BackupConfig(
        hostname = pvc.name,
        # TODO: extract excludes from pvc annotations
    )

def backup_mounted_pvc_from_pod(pvc, pv, pod, rbc):
    node_name = pod.spec.nodeName
    node_path = f"/var/lib/kubelet/pods/{pod.metadata.uid}/volumes/kubernetes.io~csi/{pv.name}/mount/"
    run_backup_pod(f"backup-{pvc.name}", node_name, node_path, rbc)

# wrapper around kubectl because kr8s does not support pod exec: https://github.com/kr8s-org/kr8s/issues/169
def pod_exec(pod: Pod, container: str, command: List[str]) -> (str, str):
    namespace = pod.namespace
    pod_name = pod.name
    cmd = ["kubectl", "exec", "-n", namespace, pod_name, "-c", container, "--"] + command
    print(f"cmd: {cmd}")
    proc = subprocess.run(cmd,
                   capture_output=True,
                   check=True,
                   text=True,
                   )
    return proc.stdout, proc.stderr

# implements logic for parsing nodeAffinity
def get_node_from_pv(pv) -> str:
    if hasattr(pv.spec, "nodeAffinity"):
        for node_selector in pv.spec.nodeAffinity.required.nodeSelectorTerms:
            for exp in node_selector.matchExpressions:
                if exp.key == "kubernetes.io/hostname" and exp.operator == "In":
                    return exp['values'][0]

    raise Exception(f"Unable to determine node for pv: {pv}")


def get_pvc_from_pv(pv) -> (str, str):
    return (pv.spec.claimRef.name, pv.spec.claimRef.namespace)

def backup_hostpath_volume(pv, rbc, pvc_name):
    path = str
    if hasattr(pv.spec, "hostPath"):
        path = pv.spec.hostPath.path
    elif hasattr(pv.spec, "local"):
        path = pv.spec.local.path
    else:
        raise Exception("Wrong pv type for backup_hostpath_volume")

    # lookup the nodeAffinity and parent pvc
    node_name = get_node_from_pv(pv) # better use volume.kubernetes.io/selected-node annotation on pvc?

    # run pod on the node mounting hostPath
    run_backup_pod(f"backup-{pvc_name}", node_name, path, rbc)


def build_restic_cmd(bc) -> List[str]:
    cmd = ["sh", "-cex"]

    # https://restic.readthedocs.io/en/stable/040_backup.html
    restic_cmd = f"restic backup --one-file-system --host {bc.hostname} --no-scan /data"
    if bc.exclude_caches:
        restic_cmd += " --exclude-caches"
    for e in bc.excludes:
        restic_cmd += f" --exclude {e}"
    for t in bc.tags:
        restic_cmd += f" --tag {t}"

    cmd.append(restic_cmd)

    return cmd

def get_env_from_secret(secret_name, namespace_name) -> dict[str,str]:
    secret = Secret.get(secret_name, namespace=namespace_name)
    env = {}
    for k,v in secret.raw["data"].items():
        env[k] = b64decode(v)

    return env

def initialize_repo():
    # TODO: probably this should be run in the container as well so it uses the same restic version

    # https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html
    print(f"Ensuring repository backend is initialized")
    env = get_env_from_secret(BACKUP_SECRET_NAME, BACKUP_NAMESPACE)
    proc = subprocess.run(["restic snapshots --no-cache"], shell=True, check=False, env=env, capture_output=True)
    if proc.returncode == 0:
        print("Repository already initialized")
        return

    print("Repository needs to be initialized")
    if not DRY_RUN:
         subprocess.run(["restic init --no-cache"], shell=True, check=True, env=env)

def get_pod_mounting_pvc(pvc):
    # iterate over all running pods in the same namespace as the PVC
    for pod in kr8s.get("pods", namespace=pvc.namespace, field_selector="status.phase=Running"):
        # check for matching 'volume'
        for volume in pod.spec.volumes:
            if hasattr(volume, "persistentVolumeClaim") and \
               hasattr(volume.persistentVolumeClaim, "claimName") and \
               volume.persistentVolumeClaim.claimName == pvc.name:
                # return the first match
                return pod

def get_pv_for_pvc(pvc):
    pv = PersistentVolume.get(pvc.spec.volumeName)
    return pv

def get_common_labels():
    return {
        "app.kubernetes.io/name": "kub-vol-bak",
        "app.kubernetes.io/instance": f"{EXECUTION_ID}",
    }

# support for PVCs that are (in order):
# - backed by a "local" PV
# - backed by a "hostPath" PV
# - mounted by a running Pod
def backup_any_pvc(pvc):
    annotation_key = "backup-enabled"
    if pvc.annotations.get(annotation_key) == "false":
        print(f"Ignoring PVC {pvc.namespace}/{pvc.name} due to annotation '{annotation_key}=false'")
        return

    # figure out how we can access the volume:
    pv = get_pv_for_pvc(pvc)
    mounting_pod = get_pod_mounting_pvc(pvc)

    # TODO: get excludes from PVC annotations
    restic_config = ResticBackupConfig(
        hostname = pvc.name,
        # repository = os.environ["RESTIC_REPOSITORY"],
        # password = os.environ["RESTIC_PASSWORD"],
        tags = [f"namespace={pvc.namespace}", f"persistentvolumeclaim={pvc.name}", f"persistentvolume={pv.name}"]
    )

    if hasattr(pv.spec, "local"):
        print(f"Backing up PVC {pvc.namespace}/{pvc.name} with 'local' strategy")
        if not DRY_RUN:
            backup_hostpath_volume(pv, restic_config, pvc.name)
    elif hasattr(pv.spec, "hostPath"):
        print(f"Backing up PVC {pvc.namespace}/{pvc.name} with 'hostPath' strategy")
        if not DRY_RUN:
            backup_hostpath_volume(pv, restic_config, pvc.name)
    elif mounting_pod:
        print(f"Backing up PVC {pvc.namespace}/{pvc.name} from running Pod {mounting_pod.name}")
        if not DRY_RUN:
            backup_mounted_pvc_from_pod(pvc, pv, mounting_pod, restic_config)
    else:
        raise Exception(f"Unable to determine backup strategy for PVC {pvc.namespace}/{pvc.name}")

    if not DRY_RUN:
        pvc.annotate({"last-successful-backup-timestamp": datetime.now().isoformat()})

def backup_all_pvcs(pvc_label_selectors):
    pvcs = kr8s.get("persistentvolumeclaims", namespace=kr8s.ALL, label_selector=pvc_label_selectors)
    for pvc in pvcs:
        backup_any_pvc(pvc)

def main(args):
    if args.dry_run is True:
        print("RUNNING ALL OPERATIONS IN DRY-RUN MODE")
        global DRY_RUN
        DRY_RUN = True

    if args.skip_repo_init is True:
        print("Warning: skipping repository initialization")
    else:
        initialize_repo()

    global BACKUP_NAMESPACE
    BACKUP_NAMESPACE = args.namespace

    global EXECUTION_ID
    EXECUTION_ID = args.execution_id

    global VOLUME_BACKUP_TIMEOUT
    VOLUME_BACKUP_TIMEOUT = int(args.volume_backup_timeout)

    global BACKUP_IMAGE
    BACKUP_IMAGE = args.image

    pvc_label_selectors: Dict[str,str]= {}
    if args.pvc_label_selector:
        labels = args.pvc_label_selector.split(",")
        for l in labels:
            parts = l.split("=", maxsplit=1)
            if len(parts) == 2:
                pvc_label_selectors[parts[0]] = parts[1]
            else:
                pvc_label_selectors[parts[0]] = ""

    if args.action == "backup":
        backup_all_pvcs(pvc_label_selectors)

        if args.cleanup:
            for pod in kr8s.get("pods", namespace=BACKUP_NAMESPACE, label_selector=get_common_labels()):
                pod.delete()
    else:
        print(f"Error: unsupported action '{args.action}'")
        sys.exit(1)



if __name__ == "__main__":
    """ This is executed when run from the command line """
    parser = argparse.ArgumentParser()

    parser.add_argument("action",
                        help="One of: backup, ",
                        default="backup",
                        )

    parser.add_argument("--dry-run",
                        action="store_true",
                        help="Do not perform any actions, only simulate them.",
                        default=False,
                        )

    parser.add_argument("--skip-repo-init",
                        action="store_true",
                        help="Do not ensure that the repository has been initialized. Only use this when you know what you are doing.",
                        default=False,
                        )

    parser.add_argument("--namespace",
                        action="store",
                        help="The namespace in which backup jobs should be run.",
                        default=BACKUP_NAMESPACE,
                        )

    parser.add_argument("--execution-id",
                        action="store",
                        help="A unique identifier for this backup job invocation.",
                        default=EXECUTION_ID,
                        )

    parser.add_argument("--volume-backup-timeout",
                        action="store",
                        help="Maximum runtime for the backup of a single volume (in seconds).",
                        default=VOLUME_BACKUP_TIMEOUT,
                        )

    parser.add_argument("--config-secret",
                        action="store",
                        help="Name of the Secret that contains the credentials for connecting to remote repositories and other configuration related to restic.",
                        default=BACKUP_SECRET_NAME,
                        )

    parser.add_argument("--cleanup",
                        action="store_true",
                        help="Remove backup pods and other temporary resources after successful completion.",
                        default=True,
                        )

    parser.add_argument("--image",
                        action="store",
                        help="The image that should be used for the backup-runner pod (must contain at least restic binary and a shell).",
                        default=BACKUP_IMAGE,
                        )

    parser.add_argument("--pvc-label-selector",
                        action="store",
                        help="Additional filtering that should be applied to find candidate PVCs",
                        )

    # # Optional verbosity counter (eg. -v, -vv, -vvv, etc.)
    # parser.add_argument(
    #     "-v",
    #     "--verbose",
    #     action="count",
    #     default=0,
    #     help="Verbosity (-v, -vv, etc)")

    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s (version {version})".format(version=__version__))

    args = parser.parse_args()
    main(args)
