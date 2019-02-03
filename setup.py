from setuptools import find_packages, setup

setup(
    name='OAuth-Client-Bridge',
    version='1.1.1',
    url='https://github.com/adamcik/oauthclientbridge',
    license='Apache License, Version 2.0',
    author='Thomas Adamcik',
    author_email='thomas@adamcik.no',
    description='Bridge OAuth2 Authorization Code Grants to Clients Grants.',
    long_description=open('README.rst').read(),
    packages=find_packages(),
    include_package_data=True,
    zip_safe=False,
    install_requires=[
        'cryptography',
        'Flask>=0.11',
        'pyprometheus',
        'requests',
    ],
    classifiers=[
        'Development Status :: 5 - Production/Stable',
        'License :: OSI Approved :: Apache Software License',
        'Operating System :: OS Independent',
        'Programming Language :: Python :: 2',
    ],
)
