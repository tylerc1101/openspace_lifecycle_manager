#!/bin/bash

BASE_DIR="/var/lib/libvirt/config/vm_config/opnsense"

if [[ ! -f ${BASE_DIR}/config.xml ]]; then
    echo "Unable to find configuration at ${BASE_DIR}/config.xml.. validate that configuration script has been executed."
    exit 1
fi

echo "Generating Configuation ISO.."
genisoimage -output "${BASE_DIR}/opnsense_config.iso" -volid opnsense-config -joliet -rock -graft-points "/conf/config.xml=${BASE_DIR}/config.xml"

if [[ $? -ne 0 ]]; then
    echo "Failed to create ISO. Quiting."
    exit 1
fi

echo "Creating OPNSense VM.."
virt-install \
  --name opnsense \
  --ram 8192 \
  --vcpu 4 \
  --cpu host-passthrough \
  --disk size=100 \
  --disk ${BASE_DIR}/opnsense_config.iso,device=cdrom,bus=ide \
  --cdrom /var/lib/libvirt/config/images/OPNsense-24.7-dvd-amd64.iso \
  --os-variant=freebsd14.0 \
  --network bridge=br-mgmt,model=virtio \
  --network bridge=br-wan,model=virtio \
  --network bridge=br-iDRAC,model=virtio \
  --noautoconsole \
  --autostart

if [[ $? -ne 0 ]]; then
    echo "VM Creation failed. Quitting."
    exit 1
fi

echo "Waiting for boot... (3mins)"
sleep 180

echo "Logging in..."
# Login Prompt
virsh send-key opnsense --codeset linux KEY_I KEY_N KEY_S KEY_T KEY_A KEY_L KEY_L KEY_E KEY_R KEY_ENTER
# Password Prompt (Default installer credentials)
virsh send-key opnsense --codeset linux KEY_O KEY_P KEY_N KEY_S KEY_E KEY_N KEY_S KEY_E KEY_ENTER
sleep 5

echo "Configuring.."
# Language Confirmation (default)
virsh send-key opnsense --codeset linux KEY_ENTER
# Select Import Config
virsh send-key opnsense --codeset linux KEY_DOWN KEY_DOWN KEY_DOWN KEY_ENTER
sleep 5
# Select cd1 Device For Config Import
virsh send-key opnsense --codeset linux KEY_DOWN KEY_DOWN KEY_ENTER
sleep 5

echo "Installing to Disk..."
# Confirm Config Import. Select Installation.
virsh send-key opnsense --codeset linux KEY_ENTER KEY_ENTER
sleep 5
# Select XFS Installation.
virsh send-key opnsense --codeset linux KEY_ENTER
# Select Disk and Confirm.
virsh send-key opnsense --codeset linux KEY_SPACE KEY_ENTER
# Accept Warning. Start Install.
virsh send-key opnsense --codeset linux KEY_LEFT KEY_ENTER


echo "Finishing Install.. (3mins)"
# Wait For Install to Complete.
sleep 180
# Select Finish Install. (Skip Password Reset).
virsh send-key opnsense --codeset linux KEY_DOWN KEY_ENTER

echo "Shutting Down VM.."
# Waiting for shutdown completion.
sleep 30

echo "Ejecting Config ISO.."
# Remove Config Disk
virsh change-media opnsense hdb --eject

echo "Starting VM.. (2mins)"
# Start VM
virsh start opnsense
sleep 120

# Login to Console
echo "Logging In.."
# Entering Default Credentials
virsh send-key opnsense --codeset linux KEY_R KEY_O KEY_O KEY_T KEY_ENTER
virsh send-key opnsense --codeset linux KEY_K KEY_R KEY_A KEY_T KEY_O KEY_S
virsh send-key opnsense --codeset linux --holdtime 500 KEY_LEFTSHIFT KEY_MINUS
virsh send-key opnsense --codeset linux KEY_O KEY_P KEY_N KEY_S KEY_E KEY_N KEY_S KEY_E
virsh send-key opnsense --codeset linux --holdtime 500 KEY_LEFTSHIFT KEY_MINUS
virsh send-key opnsense --codeset linux KEY_T KEY_E KEY_M KEY_P KEY_ENTER
sleep 5

echo "Entering Shell.."
# Select "8" to Enter Shell.
virsh send-key opnsense --codeset linux KEY_8 KEY_ENTER

# Renew Cert
echo "Renewing Self-Signed Cert.."
sleep 5
# Entering "configctl webgui restart renew" In Shell.
virsh send-key opnsense --codeset linux KEY_C KEY_O KEY_N KEY_F KEY_I KEY_G KEY_C KEY_T KEY_L KEY_SPACE
virsh send-key opnsense --codeset linux KEY_W KEY_E KEY_B KEY_G KEY_U KEY_I KEY_SPACE
virsh send-key opnsense --codeset linux KEY_R KEY_E KEY_S KEY_T KEY_A KEY_R KEY_T KEY_SPACE
virsh send-key opnsense --codeset linux KEY_R KEY_E KEY_N KEY_E KEY_W KEY_ENTER
sleep 5

echo "Exiting Shell.."
# Ctrl + D To Exit Shell and return to Default Menu.
virsh send-key opnsense --codeset linux --holdtime 500 KEY_LEFTCTRL KEY_D

echo "Installation Complete."
