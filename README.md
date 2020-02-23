
# Kujenga

Kujenga is a lightweight way to build EC2 images using Python, Boto3
and Fabric 2.

Kujenga uses simple JSON based scripts as recipies to build images in
Amazon EC2.  It does this by spinning up a base instance, uploading
any local files, and then executing a set of commands to configure the
instance.  Then it snapshots the instance to create the image. 

During the process it generates temporary keys and temporary security
groups that are used to connect to the running instance using Fabric.
After the image has been created, the instance is terminated and
the temporary keys and security groups are deleted.

Kujenga attempts to deal with the unpredictable delays and eventual 
consistancy issues of EC2.  It can be used in an automated stack or 
as a command line tool.

Finally note that Kujenga may not work with EC2 classic as it depends
on having a default VPC within which to create the temporary security
group.

## Prerequisites
Kujenga requires
  - Python 3 (tested on Python 3.7)
  - Boto3    (tested on 1.11.14)
  - Fabric   (tested on 2.5.0)

Using Amazon EC2 requires having an account there. Kujenga now leverages
boto3's features to read your 'AWS_ACCESS_KEY_ID' and 'AWS_SECRET_ACCESS_KEY'
from ~/.aws/credentials and region spec from ~/.aws/config


## Installation
TODO:

## Details

An example of a minimal Kujenga recipe is the following:

    {
        "name": "dbm-desktop",
        "description": "Desktop image in cloud for use with remote desktop",
        "region" "us-west-2",
        "user": "ubuntu",
        "instance_type": "m3.medium",
        "base_image": {
            "doc-string": "These are the U14.04 amd64 ebs images as of 3-Aug-2014",
            "us-west-2": "ami-9986fea9",
            "us-west-1": "ami-79b4b73c",
            "us-east-1": "ami-a427efcc",
            "sa-east-1": "ami-fb8b22e6"
        },
        "volume_size": 20,
        "uploads": {
            "doc-string": "Everthing in the source dir will be uploaded to target_directory",
            "source": "/home/ubuntu/source",
            "target": "/home/ubuntu/target"
        },
        "commands": [ "apt-get update",
                      "apt-get upgrade -y",
                      "apt-get install emacs -y",
                      "python /home/ubuntu/target/get_pip.py"]
    }

The **name** and **description** values will be used to annotate the
image in EC2.  Kujenga will use the EC2 region specified in the
~/.aws/config file for the region to create the image.  The region in
the json recipe is used to select the correct base image type.  The
**user** value is used to ssh into the running instance in order to
configure it.  It must be part of the sudo'ers group. For Ubuntu
images, this is the user 'ubuntu'.

The **instance_type** value will be used as the machine type for the
instance.  Note that it must be compatible with the ami that is
specified in the **base_image** dictionary. If you specify an instance
type that is not available in the specified region, you will get an
incrutable error message from EC2 complaining about an unsupported
configuration. **volume_size** specifies the size of the root volume.

The **uploads** dictionary gives directions on files to upload.
Everything in the source directory (full path) will be uploaded to the
target directory (full path). These files can then be referred to in
the commands section for installation of custom software etc.

Every entry in the **commands** section will be executed verbatim by
the sudo command.

Finally, Kujenga snapshots the instance to make a new ami and cleans up
by shutting down the instance and deletes temporary keys and security groups
