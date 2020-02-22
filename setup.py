from distutils.core import setup


setup(name             = 'kujenga',
      version          = '0.2.0',
      packages         = ["kujenga"],
      install_requires = ['boto3', 'fabric'],
      author           = 'Don MacMillen',
      author_email     = 'don@macmillen.net',
      url              = 'https://github.com/macd/kujenga',
      description      = "Ultra-lightweight EC2 ami builds from json recipes",
      license          = 'MIT',
      keywords         = 'Amazon EC2, Boto3, Fabric',
      scripts          = ["bin/kujenga"],
      zip_safe         = True,
      classifiers=[
                     'Development Status :: 3 - Alpha',
                     'Programming Language :: Python',
                     'Intended Audience :: Developers',
                     'Intended Audience :: System Administrators',
                     'License :: OSI Approved :: MIT License',
                     'Operating System :: OS Independent',
                     'Topic :: Software Development',
      ],
)
