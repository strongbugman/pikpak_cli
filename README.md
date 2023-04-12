CLI for Pikpak(A web file driver)

# Features

* List all file in a tree
* Download a whole folder
* Download files by file name or size matching
* Download resume(which will create a .part file before finished)

# Install

```shell
pip install -U pikpak_cli
```

# Usage

```
pikpak_cli
Current account: ******
Default download dir: ****
session file: .pikpak.session
try typing help
pikpak_cli>help
login
Login account
usage: login [-h] [--password PASSWORD] account [account ...]

exit
Exit cli
usage: exit [-h]

shell

usage: shell [-h]

ls
List current dir files
usage: ls [-h] [--without_audit] [--trash] [--recursion] name [name ...]

cd
Change directory
usage: cd [-h] name [name ...]

download
Download a file or many files in a directory
usage: download [-h] [--includes INCLUDES] [--excludes EXCLUDES] [--dir DIR] [--size SIZE] [--relative_path] [--new_file_name NEW_FILE_NAME] name [name ...]

pwd
Get current path
usage: pwd [-h]

du
Get files's total size
usage: du [-h] name [name ...]

help
Get help information
usage: help [-h]

config
Set default download dir or
usage: config [-h] [--downlaod_dir DOWNLAOD_DIR]

info
Print session info
usage: info [-h]

rm

usage: rm [-h] [--no_trash] name [name ...]
```

try `help`!

## examples

### ls

```shell
pikpak_cli>ls .
```

try `--recursion`!

### cd

```shell
pikpak_cli>cd Movie
pikpak_cli>pwd
pikpak_cli>/Movie
```

```shell
pikpak_cli>cd ..
pikpak_cli>
```

### download

download files only bigger than 500M to `/mnt` with a flat structure:

```shell
pikpak_cli>download Movie --size 500M --relative_path --dir /mnt
```

download all mp4 or mkv files to default download dir:

```shell
pikpak_cli>download Movie --includes *.mp4,*.mkv
```
