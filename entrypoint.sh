#!/bin/bash
set -e

mkdir -p /recordings

echo "username=$SMB_USERNAME" > /tmp/smb.cred
echo "password=$SMB_PASSWORD" >> /tmp/smb.cred
chmod 600 /tmp/smb.cred

echo "Mounting $SMB_PATH -> /recordings"
mount -t cifs "$SMB_PATH" /recordings -o credentials=/tmp/smb.cred,uid=0,gid=0,file_mode=0755,dir_mode=0755,vers=3.0 \
  || { echo "ERROR: CIFS mount failed. Check SMB_PATH, credentials, and NAS connectivity."; exit 1; }

echo "Mount successful."
exec python monitor.py
