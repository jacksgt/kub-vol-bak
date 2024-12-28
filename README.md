# kub-vol-bak: Simple Kubernetes Volume Backups with Restic

Opinionated, straightforward backups for different Kubernetes volumes types. No CRDs, no daemons.

## Installation

### Local

Due to its simplicity, `kub-vol-bak` can be run locally easily.
Checkout this repository, install the Python dependencies and you can start the first backup:

```sh
# get source code
git clone https://github.com/jacksgt/kub-vol-bak.git
cd kub-vol-bak

# install Python dependencies
python3 -m venv venv
source venv/bin/activate
pip3 install -r requirements.txt

# provide k8s cluster connection details
export KUBECONFIG=...

# provide backup storage details
kubectl create namespace kub-vol-bak
kubectl -n kub-vol-bak create secret generic kub-vol-bak-credentials \
    --from-literal=RESTIC_PASSWORD=hunter.2 \
    --from-literal=RESTIC_REPOSITORY=b2:my-bucket

# take backups!
./kub-vol-bak.py backup --pvc-label-selector app=frontend --dry-run
```

### Helm

A Helm chart is available for deploying the tool with a `CronJob` into a Kubernetes cluster.

TODO: publish as OCI image

## Set up backup storage backend

TODO: https://restic.readthedocs.io/en/latest/030_preparing_a_new_repo.html

## FAQ

## How does it work?

The `kub-vol-bak.py` Python tool implements all the application logic:

* discovers of PVCs in the Kubernetes cluster
* determines backup strategy for each PVC (depending on the type of PVC, a different mount strategy needs to be used)
* spawns pods to take backups of each PVC with [restic](https://github.com/restic/restic/)
* monitors the pod and reports on its status

### How to disable/pause backups?

A particular volume can be excluded from being backed up by adding the `backup-enabled: "false"` annotation, like this:

```sh
kubectl annotate pvc/<NAME> backup-enabled=false
```

### How do I restore data?

There are no automatic restore procedures.
Copy the environment variables from the `kub-vol-bak-credentials` secret and export them in your local shell session.
Then, `restic` CLI can be used to restore the data locally: <https://restic.readthedocs.io/en/latest/050_restore.html>

### What does `Exception: Unable to determine backup strategy for PVC namespace/name` mean?

Most likely this means that this PVC is backed by a CSI driver and the volume is currently not mounted on any node. To resolve the issue, spin up a simple pod that uses the PVC, which will force the kubelet to mount the volume on one node.

## Development

Set up development environment:

```sh
python3 -m venv venv

source venv/bin/activate
# for Fish shell, use instead:
source venv/bin/activate.fish

python3 -m ensurepip

pip3 install -r requirements.txt
```

## TODO

- implement cleanup job
- publish Helm chart with OCI image
- add more type annotations
- improve logging (debug,info,warning,error)
- setup pylint + mypy
- automate building container image to GHCR
- add license
- notifications with apprise
