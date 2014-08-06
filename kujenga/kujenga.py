#!/usr/bin/python
import boto
import boto.ec2 as ec2
from fabric.api import env, put, sudo
import functools
from glob import glob
import json
import os
import os.path
import random
import signal
import string
import time

DEBUG = True


def printdb(msg):
    if DEBUG:
        print msg


class BadConfigFile(Exception):
    pass


class TimeoutError(Exception):
    pass


def timeout(seconds, error_message='Function call timed out'):
    def decorated(func):
        def _handle_timeout(signum, frame):
            raise TimeoutError(error_message)

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
    name = prefix + "".join(random.choice(string.ascii_letters)
                            for i in range(length))
    return name


def check_configs(config):
    rq_keys = ["name", "description", "region", "user", "instance_type",
               "base_image", "uploads", "commands"]
    error = False
    for key in rq_keys:
        if key not in config:
            print("Error: missing {} key in config file".format(key))
            error = True
    if error:
        raise BadConfigFile


#
# We only use this in wait_for_running and in the initial
# commands.  After the start up period we use just the bare
# fabric sudo.  If that fails, then we want to know about it
#
@timeout(600, "'robust_sudo' (ha!) timed out")
def robust_sudo(cmd):
    while True:
        try:
            sudo(cmd)
            break
        except:
            time.sleep(10)


class Credentials(object):
    def __init__(self):
        # garbage coming through as last character ?
        # If your credentials don't work, try removing the
        # "[:-1]" from the following two lines.
        self.id = os.environ["AWS_ACCESS_KEY_ID"][:-1]
        self.key = os.environ["AWS_SECRET_ACCESS_KEY"][:-1]
        self.creds = {"aws_access_key_id": self.id,
                      "aws_secret_access_key": self.key}


class BuildContext(object):
    def __init__(self, config_params):
        self.creds = Credentials()
        self.name = config_params["name"]
        self.desc = config_params["description"]
        self.region = config_params["region"]
        self.instance_type = config_params["instance_type"]
        self.ami = config_params["base_image"][self.region]
        self.conn = ec2.connect_to_region(self.region, **(self.creds.creds))
        self.key_file_name = self.make_new_key()
        self.sec_group_name = self.make_new_grp()
        printdb("build context created")

    def make_new_key(self):
        key_file_name = random_name("tmp_key_", 10)
        key = self.conn.create_key_pair(key_file_name)
        key.save(".")
        printdb("New key {} created".format(key_file_name))
        return key_file_name

    def make_new_grp(self):
        grp_name = random_name("tmp_grp_", 10)
        grp = self.conn.create_security_group(
            grp_name,
            "Temporary group for image builds"
        )
        # Now allow ssh access
        try:
            grp.authorize("tcp", 22, 22, "0.0.0.0/0")
        except:
            print("Hmmmm, this seems to sometimes throw an exception?")

        # Save the grp object too for debug access
        self.sec_grp = grp
        printdb("new security group {} created".format(grp_name))
        return grp_name

    def spinup(self):
        self.reservation = self.conn.run_instances(
            self.ami,
            key_name=self.key_file_name,
            instance_type=self.instance_type,
            security_groups=[self.sec_group_name]
        )
        printdb("Instance {} launched".format(self.reservation.instances))
        # We are not launching more than one, so grab the first
        self.instance_obj = self.reservation.instances[0]

    def _is_in_state(self, state):
        self.instance_obj.update()
        printdb("Instance state is {}".format(self.instance_obj.state))
        if self.instance_obj.state == state:
            return True
        else:
            return False

    @timeout(300, 'Waiting for instance to enter state timed out')
    def _wait_for_state(self, state):
        while True:
            if self._is_in_state(state):
                # ip address is now available
                self.ip_addr = self.instance_obj.ip_address
                printdb("Instance IP: {}".format(self.ip_addr))
                break
            printdb("Waiting for {} at time {}".format(state, time.ctime()))
            time.sleep(15)

    def wait_for_running(self):
        self._wait_for_state("running")

    def wait_for_terminated(self):
        self._wait_for_state("terminated")

    def create_image(self):
        self.image_id = self.instance_obj.create_image(
            name=self.name,
            description=self.desc
        )
        self.image = self.conn.get_image(self.image_id)

    def is_image_complete(self):
        self.image.update()
        if self.image.state == "available":
            return True
        else:
            return False

    @timeout(1800, 'Wating for image completion timed out')
    def wait_for_image(self):
        while True:
            if self.is_image_complete():
                break
            fstrg = "Waiting for image creation at time {}"
            printdb(fstrg.format(time.ctime()))
            time.sleep(15)

    def teardown(self):
        self.conn.terminate_instances(
            [self.instance_obj.id]
        )
        self.wait_for_terminated()
        self.conn.delete_key_pair(self.key_file_name)
        self.conn.delete_security_group(self.sec_group_name)
        os.remove(self.key_file_name + ".pem")
        printdb("BuildContext teardown complete")


class ConfigInstance(object):
    def __init__(self, config_params, ip_addr, key_file_name):
        self.user = config_params["user"]
        self.target_dir = config_params["uploads"]["target"]
        env["host_string"] = "{}@{}:22".format(self.user, ip_addr)
        env["key_filename"] = "./{}.pem".format(key_file_name)
        self.key_file_name = key_file_name
        self.ip_addr = ip_addr
        self.upload_files(config_params["uploads"])
        self.do_commands(config_params["commands"])

    def upload_files(self, uploads):
        src = uploads["source"]
        if not src:
            src = os.path.abspath(".")

        file_list = glob("{}/*".format(src))
        robust_sudo("mkdir -p {}".format(self.target_dir))
        robust_sudo("chown {}:{} {}".format(self.user, self.user,
                                            self.target_dir))
        if file_list:
            for fl in file_list:
                put(fl, self.target_dir)
        else:
            printdb("Info: no files to upload to instance")

    def do_commands(self, commands):
        for cmd in commands:
            sudo(cmd)


def main(config_file):
    """
    main method of kujenga. Takes single filename argument of the json
    formatted build recipe.  See recipes in recipe directory
    """
    with open(config_file, "r") as fd:
        config_dict = json.load(fd)
        check_configs(config_dict)

    bldx = BuildContext(config_dict)
    bldx.spinup()
    bldx.wait_for_running()
    inst = ConfigInstance(config_dict, bldx.ip_addr, bldx.key_file_name)
    bldx.create_image()
    bldx.wait_for_image()
    bldx.teardown()
