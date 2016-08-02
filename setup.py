from setuptools import setup

setup(
    name='OAuth-Client-Bridge',
    version='1.0.0',
    url='https://github.com/adamcik/oauthclientbridge',
    license='Apache License, Version 2.0',
    author='Thomas Adamcik',
    author_email='thomas@adamcik.no',
    description='Bridge OAuth2 Authorization Code Grants to Clients Grants.',
    long_description=open('README.rst').read(),
    py_modules=['oauthclientbridge'],
    include_package_data=True,
    zip_safe=False,
    install_requires=[
        'cryptography',
        'Flask',
        'requests',
    ],
    classifiers=[
        'Development Status :: 4 - Beta',
        'License :: OSI Approved :: Apache Software License',
        'Operating System :: OS Independent',
        'Programming Language :: Python :: 2',
    ],
)
