from setuptools import setup, find_packages

with open("requirements.txt") as f:
    install_requires = f.read().strip().split("\n")

setup(
    name="dsi_catalogue",
    version="0.0.1",
    description="DSI Product Catalogue - Manage product catalogue sync and website publishing",
    author="DSI",
    author_email="dev@designershaik.com",
    packages=find_packages(),
    zip_safe=False,
    include_package_data=True,
    install_requires=install_requires
)
