# bdcom_olt.py
import re

class BdcomOLT:
    def __init__(self, helper, client):
        self.helper = helper
        self.client = client

    def run_flowchart(self):
        # Disable BDCOM terminal pagination
        self.helper.send_cmd('terminal length 0')

        # STEP 1: Show VLAN MAC table
        capture_mac = self.helper.send_cmd('show mac address-table vlan ' + self.client.vlan)

        # Check if a MAC is learned on a GPON interface
        if 'gpon' in capture_mac.lower():
            # msg = 'Client MAC Found on VLAN ' + self.client.vlan + ' | OLT: ' + self.client.olt_name + ' | Port: gpon' + self.client.pon_port

            lines = capture_mac.strip().splitlines()
            msg = '\n'.join(lines[:-1])
            print(msg)
            self.helper.copy_msg_and_exit('MAC Found (BDCOM)', 'CLI Output:\n', msg)
            return

        # STEP 2: MAC Not Found -> Call mac_not_found_flow
        self.mac_not_found_flow()


  
    def mac_not_found_flow(self):
        # 1. Clean port and onu_id (e.g. "0/15" and "43")
        raw = self.client.pon_port.strip()
        for prefix in ['gpon', 'GPON', 'epon', 'EPON']:
            raw = raw.replace(prefix, '')
        raw = raw.strip()

        if ':' in raw:
            port, onu_id = raw.split(':', 1)
        elif ' ' in raw:
            parts = raw.split()
            port, onu_id = parts[0], parts[1] if len(parts) > 1 else ''
        else:
            port, onu_id = raw, ''
        port, onu_id = port.strip(), onu_id.strip()

        # 2. Check ONU Online/Offline status (Note: space between port and onu_id)
        if port and onu_id:
            info_cmd = 'show gpon onu-information interface gpon ' + port + ' ' + onu_id
        else:
            info_cmd = 'show gpon onu-information sn ' + self.client.sn

        info_data = self.helper.send_cmd(info_cmd)
        info_lower = info_data.lower()

        is_online = ('online' in info_lower or 'working' in info_lower) and ('off-line' not in info_lower)




        # -------------------------------------------------------------
        # BRANCH A: ONU is OFFLINE -> Check Inactive Reason (LOSi vs Power)
        # -------------------------------------------------------------
        if not is_online:
            # Query inactive-onu table to get the exact deactivation reason
            reason_cmd = 'show gpon inactive-onu interface gpon ' + port + ' ' + onu_id
            inactive_data = self.helper.send_cmd(reason_cmd)
            reason_lower = inactive_data.lower()

            if 'losi' in reason_lower or 'los' in reason_lower or 'wire down' in reason_lower:
                msg = (
                    'Fiber cut on VLAN ' + self.client.vlan + '\n *TCL-Data* - Address: ' + self.client.address + '\n'                    
                )
                self.helper.copy_msg_and_exit('Fiber Cut Alert', 'ONU is OFFLINE (LOSi). Press Ctrl+C to copy:', msg)

            elif 'dying' in reason_lower or 'power' in reason_lower:
                msg = (
                    'End device power off on VLAN ' + self.client.vlan + ' (' + self.client.address + ') | '
                    'OLT: ' + self.client.olt_name + ' GPON ' + port + ':' + onu_id + ' | '
                    'SN: ' + self.client.sn + ' | Reason: DyingGasp (Power failure).'
                )
                self.helper.copy_msg_and_exit('Power Failure Alert', 'ONU is OFFLINE (DyingGasp). Press Ctrl+C to copy:', msg)

            else:
                msg = (
                    'End device for VLAN ' + self.client.vlan + ' (' + self.client.address + ') is off-line | '
                    'OLT: ' + self.client.olt_name + ' GPON ' + port + ':' + onu_id + ' | '
                    'SN: ' + self.client.sn
                )
                self.helper.copy_msg_and_exit('ONU Offline', 'ONU is OFFLINE. Press Ctrl+C to copy:', msg)
            return


         # -------------------------------------------------------------
        # BRANCH B: ONU is ONLINE -> Check Optical Power (RxPower)
        # -------------------------------------------------------------
        diag_cmd = 'show interface gpon ' + port + ':' + onu_id + ' onu optical-transceiver-diagnosis'
        diag_data = self.helper.send_cmd(diag_cmd)

        rx_power = None
        for line in diag_data.splitlines():
            line_clean = line.strip().lower()
            if 'gpon' in line_clean and ('--' not in line_clean):
                parts = line.strip().split()
                if len(parts) >= 6:
                    try:
                        rx_power = float(parts[3])
                    except (ValueError, IndexError):
                        pass

        # Check for bad fiber loss (< -27.0 dBm)
        if rx_power is not None and rx_power < -27.0:
            msg = (
                'High fiber loss (' + str(rx_power) + ' dBm) on VLAN ' + self.client.vlan + ' (' + self.client.address + ') | '
                'OLT: ' + self.client.olt_name + ' GPON ' + port + ':' + onu_id + '. Forward to fiber team.'
            )
            self.helper.copy_msg_and_exit('High Optical Loss', 'Optical Rx is below threshold (-27 dBm):', msg)
            return

        # Power is GOOD -> Check Running Config
        cfg_data = self.helper.send_cmd('show running-config interface gpon ' + port + ':' + onu_id)

        # 1. Is VLAN config present in the port config?
        if ('vlan ' + self.client.vlan) in cfg_data.lower() or ('profile vlan' + self.client.vlan) in cfg_data.lower():
            # Config is present -> Prompt to reboot ONU
            confirm = self.helper.ask_yes_no(
                'ONU is Online (' + str(rx_power) + ' dBm) and VLAN ' + self.client.vlan + ' config is present.\n\n'
                'No MAC learned. Do you want to reboot the ONU now?',
                'Reboot ONU'
            )
            if confirm:
                self.helper.send_cmd('gpon onu reboot interface gpon ' + port + ' ' + onu_id)
                self.helper.end_the_program('Reboot command sent to ONU ' + port + ':' + onu_id + '.\nPlease wait 2 minutes and check again.')
            else:
                self.helper.copy_msg_and_exit('MAC Not Learned', 'VLAN config present, reboot skipped by user.', cfg_data)

        # 2. VLAN config is MISSING -> Ask engineer before applying fix
        else:
            self.fix_bdcom_vlan_profile(cfg_data, port, onu_id)










    def fix_bdcom_vlan_profile(self, current_cfg, port, onu_id):
        """
        Creates the missing BDCOM GPON ONU VLAN profile using:
          gpon profile onu-vlan <profile-name>
          gpon-profile vlan mode tag
          gpon-profile vlan pvid <vlan_id>
        and binds it to the target ONU port.
        """
        profile_name = 'Tcl-Data-' + self.client.vlan

        prompt_msg = (
            'VLAN ' + self.client.vlan + ' config is MISSING on GPON ' + port + ':' + onu_id + '!\n\n'
            'Current Port Config:\n' + current_cfg.strip() + '\n\n'
            'Do you want to create/apply profile ' + profile_name + ' now?'
        )

        if not self.helper.ask_yes_no(prompt_msg, 'Add Missing VLAN Profile'):
            self.helper.end_the_program('Profile configuration cancelled by engineer.')
            return

        # Check if profile already exists in running-config
        prof_check = self.helper.send_cmd('show running-config | include gpon profile onu-vlan ' + profile_name)

        # Enter Global Configuration Mode
        self.helper.send_cmd('config')

        # Create profile only if it does not already exist
        if profile_name not in prof_check:
            self.helper.send_cmd('gpon profile onu-vlan ' + profile_name)
            self.helper.send_cmd('gpon-profile vlan mode tag')
            self.helper.send_cmd('gpon-profile vlan pvid ' + self.client.vlan)
            self.helper.send_cmd('exit')

        # Bind the VLAN profile to the target ONU port
        self.helper.send_cmd('interface gpon ' + port + ':' + onu_id)
        self.helper.send_cmd('gpon onu uni 1 vlan-profile ' + profile_name)
        self.helper.send_cmd('exit')
        self.helper.send_cmd('exit')

        # Verify whether the MAC address is now learned on this VLAN
        verify_mac = self.helper.send_cmd('show mac address-table vlan ' + self.client.vlan)
        self.helper.copy_msg_and_exit(
            'Profile Applied',
            'VLAN profile ' + profile_name + ' applied successfully.\nVerification MAC table output:',
            verify_mac.strip()
        )


