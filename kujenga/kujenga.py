#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Kujenga: simple EC2 image creation from json recipies"""

__all__ = ['create_image']

import functools
from glob import glob
import json
import os
import os.path
import random
import signal
import string
import time
import boto3
from botocore.exceptions import ClientError
import fabric

VERBOSE = True

def printdb(msg):
    """Print if VERBOSE is True"""
    if VERBOSE:
        print(msg)


class BadConfigFile(Exception):
    """Raise a stink when the config file is not correct"""


class KujengaTimeoutError(Exception):
    """Raise a stink when taking too long"""


def timeout(seconds, error_message='Function call timed out'):
    """A decorator for timeout (go sit on the steps!)"""
    def decorated(func):
        def _handle_timeout(signum, frame):
            raise KujengaTimeoutError(error_message)

        def wrapper(*args, **kwargs):
            signal.signal(signal.SIGALRM, _handle_timeout)
            signal.alarm(seconds)
            try:
                result = func(*args, **kwargs)
            finally:
                signal.alarm(0)
            return result

        return functools.wraps(func)(wrapper)
    return decorated


def random_name(prefix, length):
    """Generate a random name with a fixed prefix"""
    name = prefix + "".join(random.choice(string.ascii_letters)
                            for i in range(length))
    return name


def check_configs(config):
    """Check the json config file for required fields"""
    rq_keys = ["name", "description", "region", "user", "instance_type",
               "base_image", "volume_size", "uploads", "commands"]
    error = False
    for key in rq_keys:
        if key not in config:
            print("Error: missing {} key in config file".format(key))
            error = True
    if error:
        raise BadConfigFile


class BuildContext:
    """Context for interacting with EC2"""
    def __init__(self, config_params):
        self.name = config_params["name"]
        self.desc = config_params["description"]
        self.instance_type = config_params["instance_type"]
        self.volume_size = config_params["volume_size"]
        self.region = config_params["region"]
        self.ami = config_params["base_image"][self.region]
        self.ec2 = boto3.client("ec2") # creds and region in .aws
        self.keyname = None
        self.key_filename = None
        self.group_name = None
        self.group_id = None
        self.reservation = None
        self.ip_addr = None
        self.instance_id = None
        self.instance_state = None
        self.image_id = None
        self.make_new_key()
        self.make_new_grp()
        printdb("build context created")

    def make_new_key(self):
        """Make a new temporary key to use on the running instance"""
        self.keyname = random_name("tmp_key_", 10)
        self.key_filename = os.path.abspath(self.keyname + ".pem")
        key = self.ec2.create_key_pair(KeyName=self.keyname)
        with open(self.key_filename, "w") as keyfile:
            keyfile.write(key["KeyMaterial"])

        os.chmod(self.key_filename, 256)  # note that 256 is octal 400
        printdb("New key {} created".format(self.key_filename))


    def make_new_grp(self):
        """Make a new temporary security group and set access for ssh"""
        resp = self.ec2.describe_account_attributes(AttributeNames=["default-vpc"])
        vpc_id = resp["AccountAttributes"][0]["AttributeValues"][0]["AttributeValue"]
        if vpc_id == "none":
            raise Exception

        self.group_name = random_name("tmp_grp_", 10)
        grp = self.ec2.create_security_group(
            GroupName=self.group_name,
            Description="Temporary group for image builds",
            VpcId=vpc_id
        )
        self.group_id = grp["GroupId"]
        printdb("Security group {} created".format(self.group_id))
        # Now allow ssh access
        try:
            self.ec2.authorize_security_group_ingress(
                GroupId=self.group_id,
                IpPermissions=[
                    {
                        'FromPort': 22,
                        'IpProtocol': 'tcp',
                        'IpRanges': [
                            {
                                'CidrIp': "0.0.0.0/0",
                                'Description': 'all ranges'
                            },
                        ],
                        'ToPort': 22,
                    },
                ]
            )
        except Exception as _e:
            printdb(_e)

        # Save the grp object too for debug access
        printdb("new security group {} created".format(self.group_name))

    def spinup(self):
        """Spin up a new instance using ami from config json"""
        self.reservation = self.ec2.run_instances(
            BlockDeviceMappings=[
                {
                    "DeviceName": "/dev/sda1",
                    "Ebs": {
                        "VolumeSize": self.volume_size,
                        "VolumeType": "standard"
                    }
                },
            ],
            ImageId=self.ami,
            KeyName=self.keyname,
            InstanceType=self.instance_type,
            SecurityGroupIds=[self.group_id],
            MaxCount=1,
            MinCount=1,
            Monitoring={"Enabled": False}
        )
        self.instance_id = self.reservation["Instances"][0]["InstanceId"]
        printdb("Instance {} launched".format(self.instance_id))

    # OK, so this can fail even if we have just been given an good instance_id
    # so we catch that and set the state to "unknown"... eventually the EC2 database(s)
    # will be consistent (we hope)
    def _update_instance_state(self):
        try:
            inst_info = self.ec2.describe_instances(InstanceIds=[self.instance_id])
            self.instance_state = inst_info["Reservations"][0]["Instances"][0]["State"]["Name"]
        except ClientError:
            printdb("Didn't find instance {}".format(self.instance_id))
            self.instance_state = "unknown"

    def _is_in_state(self, state):
        self._update_instance_state()
        printdb("Instance state is {}".format(self.instance_state))
        return self.instance_state == state

    @timeout(300, 'Waiting for instance to enter state timed out')
    def _wait_for_state(self, state):
        while True:
            if self._is_in_state(state):
                break
            printdb("Waiting for {} at time {}, currently in {}".format(
                state, time.ctime(), self.instance_state))

            time.sleep(15)

    def wait_for_running(self):
        """Wait until instance is in the 'running' state before advancing"""
        self._wait_for_state("running")
        # ip address is now available
        inst_info = self.ec2.describe_instances(InstanceIds=[self.instance_id])
        self.ip_addr = inst_info["Reservations"][0]["Instances"][0]["PublicIpAddress"]
        printdb("Instance IP: {}".format(self.ip_addr))

    def wait_for_terminated(self):
        """Wait until instance is in the 'terminated' state before advancing"""
        self._wait_for_state("terminated")

    def create_image(self):
        """Create a EC2 image from a running instance"""
        response = self.ec2.create_image(
            Name=self.name,
            Description=self.desc,
            InstanceId=self.instance_id
        )
        self.image_id = response["ImageId"]

    def is_image_complete(self):
        """Test for completeness"""
        response = self.ec2.describe_images(
            ImageIds=[self.image_id]
        )
        state = response["Images"][0]["State"]
        printdb("Image state: {}".format(state))
        return state == "available"

    @timeout(3600, 'Image completion timed out. Teardown skipped. Remember to manually clean up!')
    def wait_for_image(self):
        """Have to wait for image to complete before cleaning up"""
        while True:
            if self.is_image_complete():
                break
            fstrg = "Waiting for image creation at time {}"
            printdb(fstrg.format(time.ctime()))
            time.sleep(15)

    def teardown(self):
        """Cleanliness is next to..."""
        self.ec2.terminate_instances(
            InstanceIds=[self.instance_id]
        )
        self.wait_for_terminated()
        self.ec2.delete_key_pair(KeyName=self.keyname)
        self.ec2.delete_security_group(GroupId=self.group_id)
        os.remove(self.key_filename)
        printdb("BuildContext teardown complete")

# So this is used to set environment vars, etc, if the file envx exists.
# That way we can manipulate the environment from the json recipe file
# by creating, adding to, or deleting the file envx on the remote host
SENV = "if [ -f envx ]; then \n source envx \n fi ;"

class ConfigInstance:
    """Do the stuff to configure the instance before creating image"""
    def __init__(self, config_params, ip_addr, key_filename):
        self.user = config_params["user"]
        self.target_dir = config_params["uploads"]["target"]
        self.conn = fabric.connection.Connection(
            host=ip_addr,
            user=self.user,
            connect_kwargs={"key_filename": key_filename}
        )

        self.upload_files(config_params["uploads"])
        self.do_commands(config_params["commands"])

    #
    # We only use this in wait_for_running and in the initial
    # commands.  After the start up period we use just the bare
    # fabric sudo.  If that fails, then we want to know about it
    #
    @timeout(600, "'robust_sudo' (ha!) timed out")
    def robust_sudo(self, cmd):
        """Wait for sudo command on remote host up to 10 minutes before throwing in the towel"""
        while True:
            try:
                self.conn.sudo(cmd)
                break
            except Exception as _e:
                time.sleep(10)

    def upload_files(self, uploads):
        """Upload all the files in the directory specified in the json recipe"""
        src = uploads["source"]
        if not src:
            src = os.path.abspath(".")

        file_list = glob("{}/*".format(src))
        self.robust_sudo("mkdir -p {}".format(self.target_dir))
        self.robust_sudo("chown {}:{} {}".format(self.user, self.user,
                                                 self.target_dir))
        if file_list:
            for fl in file_list:
                self.conn.put(fl, self.target_dir)
        else:
            printdb("Info: no files to upload to instance")


    # We catch a failed command here to try to do as much as we can in
    # one go (better for debugging the json recipe).  We used to use the
    # "sudo" method of the Connection object, but there were some corner
    # case problems.  By using the normal "run" method from the connection
    # object we can then use "sudo" in the json recipe, if necessary. By
    # doing this we can interleave normal commands and sudo commands,
    # which is sometime necessary.
    def do_commands(self, commands):
        """Now perform all the commands from the json recipe"""
        for cmd in commands:
            try:
                self.conn.run(SENV + cmd)
            except Exception as _e:
                printdb(_e)
                printdb("Command '{}' failed. Skipping\n\n".format(cmd))


def create_image(config_file, debug_build=False):
    """
    main method of kujenga. Takes single filename argument of the json
    formatted build recipe.  See recipes in recipe directory for examples
    """
    with open(config_file, "r") as fd:
        config_dict = json.load(fd)
        check_configs(config_dict)

    bldx = BuildContext(config_dict)
    bldx.spinup()
    bldx.wait_for_running()
    printdb("Configuring instance...")
    cfg = ConfigInstance(config_dict, bldx.ip_addr, bldx.key_filename)
    if not debug_build:
        bldx.create_image()
        bldx.wait_for_image()
        bldx.teardown()

    # we return these for debugging only
    return bldx, cfg
