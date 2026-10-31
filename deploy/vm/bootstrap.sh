#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu 22.04/24.04 VM (Oracle Cloud Always Free Ampere A1, or any
# arm64/amd64 host): firewall, k3s, Helm, Docker for image builds, cert-manager.
#
#   sudo bash deploy/vm/bootstrap.sh
#
# Safe to re-run.
set -euo pipefail

K3S_VERSION="v1.31.4+k3s1"
HELM_VERSION="v3.16.4"
CERT_MANAGER_VERSION="v1.16.2"

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive

echo "== packages"
apt-get update -q
apt-get install -y -q curl ca-certificates gettext-base python3 docker.io iptables-persistent openssl jq
systemctl enable --now docker

echo "== firewall"
# Oracle's Ubuntu images ship iptables rules that reject everything but SSH, including traffic
# forwarded between pods. Open HTTP/HTTPS (the Oracle security list must allow them too) and let
# k3s's pod (10.42/16) and service (10.43/16) networks through.
iptables -C INPUT -p tcp -m multiport --dports 80,443 -m conntrack --ctstate NEW -j ACCEPT 2>/dev/null \
  || iptables -I INPUT -p tcp -m multiport --dports 80,443 -m conntrack --ctstate NEW -j ACCEPT
for net in 10.42.0.0/16 10.43.0.0/16; do
  iptables -C INPUT -s "$net" -j ACCEPT 2>/dev/null || iptables -I INPUT -s "$net" -j ACCEPT
done
while iptables -D FORWARD -j REJECT --reject-with icmp-host-prohibited 2>/dev/null; do :; done
netfilter-persistent save

echo "== k3s $K3S_VERSION"
if ! command -v k3s >/dev/null; then
  # Traefik (ingress), local-path storage and the network-policy controller all come with k3s.
  curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="$K3S_VERSION" sh -s - --write-kubeconfig-mode 0640
fi
mkdir -p /home/ubuntu/.kube
cp /etc/rancher/k3s/k3s.yaml /home/ubuntu/.kube/config
chown -R ubuntu:ubuntu /home/ubuntu/.kube
chmod 600 /home/ubuntu/.kube/config
usermod -aG docker ubuntu
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
until kubectl get nodes 2>/dev/null | grep -q " Ready"; do sleep 3; done

echo "== helm $HELM_VERSION"
if ! command -v helm >/dev/null; then
  arch=$(dpkg --print-architecture)
  curl -sfL "https://get.helm.sh/helm-${HELM_VERSION}-linux-${arch}.tar.gz" | tar -xz -C /tmp
  install -m 0755 "/tmp/linux-${arch}/helm" /usr/local/bin/helm
fi

echo "== cert-manager $CERT_MANAGER_VERSION"
kubectl apply -f "https://github.com/cert-manager/cert-manager/releases/download/${CERT_MANAGER_VERSION}/cert-manager.yaml"
kubectl -n cert-manager rollout status deploy/cert-manager-webhook --timeout=300s

echo "bootstrap done; log out and back in (docker group), then run deploy/vm/deploy.sh"
