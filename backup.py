#!/usr/bin/env python3

from base64 import b64decode, b64encode
import subprocess # see also: https://pypi.org/project/python-shell/
import time
from dataclasses import dataclass, field
from datetime import datetime
import os

from typing import List, Dict, Optional

# https://docs.kr8s.org/en/latest/
import kr8s
from kr8s.objects import Pod, Secret, PersistentVolume, PersistentVolumeClaim

DRY_RUN = True
BACKUP_NAMESPACE = "kube-backup"
BACKUP_SECRET_NAME = "backup-credentials"
RESTIC_IMAGE = "docker.io/restic/restic:0.16.0"
BACKUP_TIMEOUT = 3600 # 1h
EXECUTION_ID = 1234
CLEANUP = False

# Restic integration can only backup volumes that are mounted by a pod and not directly from the PVC. For orphan PVC/PV pairs (without running pods), some Velero users overcame this limitation running a staging pod (i.e. a busybox or alpine container with an infinite sleep) to mount these PVC/PV pairs prior taking a Velero backup.
# https://velero.io/docs/v1.9/restic/

def run_backup_pod(pod_name, node_name, node_path, rbc):
    pod = Pod({
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "namespace": BACKUP_NAMESPACE,
            "labels": {
                "app.kubernetes.io/name": "backup-runner",
                "app.kubernetes.io/instance": f"{EXECUTION_ID}",
            },
        },
        "spec": {
            "serviceAccountName": "backup-runner",
            "containers": [{
                "name": "restic",
                "image": RESTIC_IMAGE,
                "volumeMounts": [
                    {"name": "data", "mountPath": "/data", "readOnly": True},
                    {"name": "tmp", "mountPath": "/tmp", "readOnly": False},
                ],
                "envFrom": [{ "secretRef": {"name": BACKUP_SECRET_NAME }, }, ],
                "command": build_restic_cmd(rbc),
                "terminationMessagePolicy": "FallbackToLogsOnError",
            }],
            "volumes": [
                {"name": "data", "hostPath": {"path": node_path, "type": "Directory"}},
                {"name": "tmp", "emptyDir": {}}
            ],
            # "terminationGracePeriodSeconds": 5,
            "restartPolicy": "Never",
            "activeDeadlineSeconds": BACKUP_TIMEOUT,
            "enableServiceLinks": False,
            "nodeName": node_name,
            "automountServiceAccountToken": False,
        },
    })

    if DRY_RUN:
       print(pod.spec)
       return

    # launch pod
    pod.create()

    # pod.ready()
    time.sleep(5)

    for line in pod.logs(follow=True, timeout=None):
        print(line)

    pod.refresh()
    print(f"Pod {pod.name} terminated: {pod.status.phase}")

    def cleanup():
       pod.delete()

    return pod, cleanup

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
    # namespace, pvc_name = get_pvc_from_pv(pv)
    # pvc = PersistentVolumeClaim(pvc_name, namespace=namespace)

    # run pod on the node mounting hostPath
    pod = run_backup_pod(f"backup-{pvc_name}", node_name, path, rbc)

    # run backup for it
    # backup_cmd = build_restic_cmd(bc)
    # pod_exec(pod, "restic", backup_cmd)

def build_restic_cmd(bc) -> List[str]:
    cmd = ["sh", "-cex"]

    # https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html
    # cmd += "if ! restic snapshots 2>&1 >/dev/null; then restic init; fi;"

    # https://restic.readthedocs.io/en/stable/040_backup.html
    restic_cmd = f"restic backup --one-file-system --host {bc.hostname} --no-scan /data"
    if bc.exclude_caches:
        restic_cmd += " --exclude-caches"
    for e in bc.excludes:
        restic_cmd += f" --exclude={e}"
    for t in bc.tags:
        restic_cmd += f" --tag={t}"

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
    proc = subprocess.run(["restic snapshots --no-cache"], shell=True, check=False, env=env)
    if proc.returncode == 0:
        print("Repository already initialized")
        return

    print("Repository needs to be initialized")
    if not DRY_RUN:
         subprocess.run(["restic init --no-cache"], shell=True, check=True, env=env)

def backup_all_pvcs():
    pvcs = kr8s.get("persistentvolumeclaims", namespace=kr8s.ALL)
    for pvc in pvcs:
        backup_any_pvc(pvc)

    if CLEANUP:
        for pod in kr8s.get("pods", namespace=BACKUP_NAMESPACE, label_selector={
            "app.kubernetes.io/name": "backup-runner",
            "app.kubernetes.io/instance": EXECUTION_ID,
            }):
            pod.delete()


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

    pvc.annotate({"last-successful-backup-timestamp": datetime.now().isoformat()})

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

SKIP_REPO_INIT = True
if __name__ == "__main__":
    if DRY_RUN is True:
        print("RUNNING ALL OPERATIONS IN DRY-RUN MODE")
    if not SKIP_REPO_INIT:
        initialize_repo()
    backup_all_pvcs()



############


# def backup_hostpath_volumes(node: str):
#     # get all pvs that have spec.hostPath
#     pv_list = kr8s.get("pvs", kr8s.ALL)
#     hostpath_pvs = [pv for pv in pv_list if pv.spec.hostPath]

#     for pv in hostpath_pvs:
#         backup_hostpath_volume(pv)
#     return

# def backup_pvc(pvc):
#     print(f"Processing PVC {pvc.namespace}/{pvc.name}")
#     bc = get_backup_config(pvc)
#     pod, cleanup_func = launch_backup_pod(pvc.namespace, f"backup-{pvc.name}", bc)
#     run_backup(bc)
#     # https://restic.readthedocs.io/en/stable/040_backup.html
#     restic_cmd = "restic backup --one-file-system --no-scan /data"
#     if bc.exclude_caches:
#         restic_cmd += " --exclude-caches"
#     for e in bc.excludes:
#         restic_cmd += " --exclude={e}"
#     print(f"Starting backup: {restic_cmd}")
#     pod_exec(pod, "restic", restic_cmd)

    # cleanup_func()

# THIS CAN BE CREATED WITH STATIC MANIFESTS
# def create_backup_secret(rbc):
#     secret_data = {
#         "RESTIC_REPOSITORY": os.environ["RESTIC_REPOSITORY"],
#         "RESTIC_PASSWORD": os.environ["RESTIC_PASSWORD"],
#     }
#     secret = Secret({
#         "apiVersion": "v1",
#         "kind": "Secret",
#         "metadata": {
#             "name": BACKUP_SECRET_NAME,
#             "namespace": BACKUP_NAMESPACE,
#         },
#         "stringData": secret_data,
#     })
#     secret.create()
#     print(f"Created backup secret {secret.name}")

#     def cleanup():
#         secret.delete()

#     return secret, cleanup

# @dataclass
# class BackupConfig:
#     hostname: str
#     repository: str
#     password: str
#     path: str
#     # node: str
#     pv_name: str
#     hostPath: Optional[str] = None
#     exclude_caches: bool = True
#     env_vars: Dict[str,str] = field(default_factory=dict)
#     excludes: List[str] = field(default_factory=list)
#     metadata: Dict[str,str] = field(default_factory=dict)

# def backup_pvcs_mounted_on_node(node_name: str, base_path: str = "/var/lib/kubelet/pods/"):
#     # run pod on the node mounting base_path
#     backup_pod_config = {
#         node: node_name,
#         mount_host_path: base_path,
#     }
#     pod = launch_backup_pod(backup_pod_config)

#     # get mounted pvs
#     mounted_pvs = pod_exec(pod, "restic", f"find {base_path} -name mount |" + r" sed -r 's|.*\/(pvc-[a-z0-9-]+)\/.*|\1|g' | sort -u")

#     for pv_name in mounted_pvs:
#         backup_mounted_pv(pv_name)
