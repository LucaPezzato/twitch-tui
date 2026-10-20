from setuptools import find_packages, setup

setup(
    name="twitch-tui",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "textual>=0.80.0",
        "rich>=13.0.0",
        "Pillow>=10.0.0",
    ],
    entry_points={
        "console_scripts": [
            "twitch-tui=twitch_tui.cli:main",
        ],
    },
)
