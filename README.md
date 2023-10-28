# kub-vol-bak: Simple Kubernetes Volume Backups with Restic


Disable backups for a PVC: `kubectl annotate pvc/<NAME> backup-enabled=false`

What does `Exception: Unable to determine backup strategy for PVC namespace/name` mean?
=> most likely this means that this PVC is backed by a CSI driver and the volume is currently not mounted on any node. To resolve the issue, spin up a simple pod that uses the PVC, which will force the kubelet to mount the volume on one node.
