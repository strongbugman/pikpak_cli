import argparse
import asyncio
import base64
import contextlib
import dataclasses
import fnmatch
import getpass
import inspect
import json
import os
import sys
import typing

import tenacity
from httpx import HTTPError
from prompt_toolkit import prompt
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.history import FileHistory
from rich import print
from rich.text import Text

from pikpak_cli.ant import Pikpak


class CliException(Exception):
    pass


@dataclasses.dataclass
class Session:
    name: str = ".pikpak.session"
    download_dir: str = ""
    current_dirs: typing.List[str] = dataclasses.field(default_factory=lambda: [""])
    current_dir_ids: typing.List[str] = dataclasses.field(default_factory=lambda: [""])
    token: typing.Dict = dataclasses.field(default_factory=dict)
    account: str = ""
    password: str = ""

    @property
    def current_dir(self):
        return "/".join(self.current_dirs) + "/"

    @property
    def current_dir_id(self):
        return self.current_dir_ids[-1]

    def load(self):
        with open(self.name, "r") as f:
            for k, v in json.load(f).items():
                setattr(self, k, v)
        self.password = base64.b64decode(self.password).decode()

    def save(self):
        with open(self.name, "w") as f:
            data = dataclasses.asdict(self)
            data["password"] = base64.b64encode(self.password.encode()).decode()
            json.dump(data, f)


@dataclasses.dataclass
class Command:
    call: typing.Callable
    name: str = ""
    parser: argparse.ArgumentParser = dataclasses.field(
        default_factory=argparse.ArgumentParser
    )

    @property
    def help_text(self):
        return (
            Text(self.name, style="gray")
            + "\n"
            + Text(self.parser.description, style="blue")
            + "\n"
            + Text(self.parser.format_usage(), style="green")
        )

    def _error(self, message: str):
        raise CliException(message)

    def __post_init__(self):
        self.name = self.call.__name__
        self.parser.prog = self.name
        self.parser.description = self.call.__doc__ or ""
        self.parser.error = self._error
        for n, v in inspect.signature(self.call).parameters.items():
            if n.startswith("_"):
                continue
            if not v.default is inspect.Signature.empty:
                if isinstance(v.default, bool):
                    self.parser.add_argument(
                        f"--{n}", default=v.default, action="store_true"
                    )
                else:
                    self.parser.add_argument(f"--{n}", default=v.default)
            else:
                self.parser.add_argument(n, nargs="+")


class Commander:
    def __init__(self) -> None:
        self.session = Session()
        with contextlib.suppress(FileNotFoundError, json.JSONDecodeError):
            self.session.load()
        self.ant = Pikpak()
        self.ant.auth_pipeline.token = self.session.token
        self.files = {}
        self.CMDS: typing.Dict[str, Command] = {}
        asyncio.ensure_future(self.refresh_token())

        for f in (
            self.login,
            self.exit,
            self.ls,
            self.cd,
            self.download,
            self.pwd,
            self.du,
            self.help,
            self.set_download_dir,
            self.info,
            self.rm,
        ):
            cmd = Command(f)
            self.CMDS[cmd.name] = cmd

    def size2str(self, num: int, suffix="B") -> str:
        for unit in ["", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"]:
            if abs(num) < 1024.0:
                return f"{num:3.1f}{unit}{suffix}"
            num /= 1024.0
        return f"{num:.1f}Yi{suffix}"

    def size2int(self, size: str) -> int:
        multiples = {}
        for i, n in enumerate(["", "K", "M", "G", "T", "P", "E", "Z", "Y"]):
            multiples[n] = pow(1024, i)

        for n in list(multiples.keys()):
            multiples[n + "B"] = multiples[n]
            multiples[n + "iB"] = multiples[n]
        for n in list(multiples.keys()):
            multiples[n.lower()] = multiples[n]

        multiple = 1
        size_num = size
        for n in multiples.keys():
            if n and size.endswith(n):
                multiple = multiples[n]
                size_num = size.replace(n, "")
                break

        try:
            return int(size_num) * multiple
        except ValueError:
            raise CliException(f"Wrong size: {size}")

    async def traverse_files(self) -> dict:
        if not self.files:
            await self._ls()

        for f in self.files.values():
            if "folder" in f["kind"]:
                await self.cd(f["name"])
                async for f in self.traverse_files():
                    yield f
                await self.cd("..")
            yield f

    def exec(self, input: str):
        if not input:
            return

        args = input.strip().replace("?", " -h").split(" ")
        if args[0] not in self.CMDS:
            if input == "?":
                self.help()
                return
            else:
                print(Text("Wrong command!", style="red"))
                return

        cmd = self.CMDS[args[0]]

        try:
            if len(args) == 2 and args[1] == "-h":
                print(cmd.help_text)
                return

            ns = cmd.parser.parse_args(args=args[1:])
            data = {}
            for k, v in ns.__dict__.items():
                if isinstance(v, list):
                    data[k] = " ".join(v)
                else:
                    data[k] = v
            res = cmd.call(**data)
            if asyncio.iscoroutine(res):
                asyncio.get_event_loop().run_until_complete(res)
        except CliException as e:
            print("input error:", Text(str(e), style="red"))
        except HTTPError as e:
            print("http error:", Text(str(e), style="red"))

    def info(self):
        """Print session info"""
        print(Text(f"Current account: {self.session.account}", style="blue"))
        print(Text(f"Current dir: {self.session.current_dir}", style="blue"))
        print(Text(f"Default download dir: {self.session.download_dir}", style="blue"))
        print(Text(f"session file: {self.session.name}", style="blue"))

    def help(self):
        """Get help information"""
        for c in self.CMDS.values():
            print(c.help_text)

    def set_download_dir(self, dir: str):
        """Set default download dir"""
        os.makedirs(dir, exist_ok=True)
        self.session.download_dir = dir

    async def login(self, account: str, password: str = "", _echo: bool = True):
        """Login account"""
        if not password:
            password = getpass.getpass("Input your password:")

        token = await self.ant.login(account, password)
        self.session.account = account
        self.session.password = password
        self.session.token = token
        self.session.save()
        if _echo:
            print(Text(f"Hello {account}!", style="blue"))

    async def refresh_token(self):
        while True:
            await asyncio.sleep(10 * 60)
            with contextlib.suppress():
                if self.session.account and self.session.password:
                    await self.login(
                        self.session.account,
                        password=self.session.password,
                        _echo=False,
                    )

    async def _ls(self):
        self.files = {
            f["name"]: f
            for f in (await self.ant.list_files(parent_id=self.session.current_dir_id))[
                "files"
            ]
        }

    async def ls(self, without_audit: bool = False, trash: bool = False):
        """List current dir files"""
        await self._ls()
        for f in self.files.values():
            if f.get("trashed") and not trash:
                continue
            if f.get("audit") and without_audit:
                continue
            print(
                Text(f["modified_time"], style="gray")
                + " "
                + Text(self.size2str(int(f["size"])), style="blue")
                + " "
                + Text(f["name"], style="green")
                + ("/" if "folder" in f["kind"] else "")
            )

    def pwd(self):
        """Get current path"""
        print(Text(self.session.current_dir, style="green"))

    async def cd(self, name: str):
        """Change directory"""
        if not self.files:
            await self._ls()

        if name == ".":
            return
        if name == "/":
            self.session.current_dir_ids = [""]
            self.session.current_dirs = [""]
            self.session.save()
            await self._ls()
            return
        if name == "..":
            if len(self.session.current_dir_ids) == 1:
                print("in root dir!")
                return
            self.session.current_dir_ids.pop()
            self.session.current_dirs.pop()
            self.session.save()
            await self._ls()
            return

        if name not in self.files:
            print(f"wrong name: {name}")
            return
        if "folder" not in self.files[name]["kind"]:
            print("not a folder!")
            return

        self.session.current_dir_ids.append(self.files[name]["id"])
        self.session.current_dirs.append(self.files[name]["name"])
        self.session.save()
        await self._ls()

    async def du(self, name: str):
        """Get files's total size"""
        if name:
            await self.cd(name)

        size = 0
        async for f in self.traverse_files():
            size += int(f["size"])

        print(Text("all size: ", "green"), Text(self.size2str(size), "blue"))

        if name:
            await self.cd("..")

    async def rm(self, name: str, no_trash: bool = False):
        if name == ".":
            file = {"kind": "folder", "id": self.session.current_dir_id, "name": "."}
        else:
            file = self.files.get(name, {})
        if not file:
            print("wrong file name!")
            return

        await self.ant.delete_file([file["id"]], trash=not no_trash)

    @tenacity.retry(
        wait=tenacity.wait_fixed(60),
        stop=tenacity.stop_after_attempt(3 + 1),
        retry=tenacity.retry_if_exception_type(HTTPError),
        reraise=True,
    )
    async def download(
        self,
        name: str,
        includes: str = "",
        excludes: str = "",
        dir: str = "",
        size: str = "",
        relative_path: bool = False,
        _dirs: typing.Sequence[str] = tuple(),
    ):
        """Download a file or many files in a directory"""
        if name == ".":
            file = {"kind": "folder", "id": self.session.current_dir_id, "name": "."}
        else:
            file = self.files.get(name, {})
        if not file:
            print("wrong file name!")
            return

        if "file" in file["kind"]:
            # filter
            if includes:
                include = False
                for p in includes.pslit(","):
                    if fnmatch.fnmatch(name, p):
                        include = True
                        break
                if not include:
                    print(
                        Text(
                            f"{file['name']} ignored by pattern {p} not matched",
                            style="green",
                        )
                    )
                    return
            if excludes:
                exclude = False
                for p in excludes.pslit(","):
                    if fnmatch.fnmatch(name, p):
                        exclude = True
                        break
                if exclude:
                    print(
                        Text(
                            f"{file['name']} ignored by pattern {p} matched",
                            style="green",
                        )
                    )
                    return
            data = await self.ant.get_file_link(self.files[name]["id"])
            if size and int(data["size"]) < self.size2int(size):
                print(
                    Text(
                        f"{file['name']}({self.size2str(int(data['size']))}) ignored by size limit",
                        style="green",
                    )
                )
                return

            if not dir:
                dir = self.session.download_dir
            if relative_path:
                filename = os.path.join(dir, name)
            else:
                filename = os.path.join(dir, *_dirs, name)
            os.makedirs(os.path.dirname(filename), exist_ok=True)
            print(Text(f"Downloading {name} to {filename}...", style="gray"))
            await self.ant.download(
                data["links"]["application/octet-stream"]["url"], filename
            )
            print(Text(f"Downloaded {name} to {filename}", style="blue"))
            await asyncio.sleep(0.5)
        else:
            await self.cd(name)
            for f in self.files.values():
                if f.get("trashed"):
                    continue
                await self.download(
                    f["name"],
                    includes=includes,
                    size=size,
                    dir=dir,
                    relative_path=relative_path,
                    _dirs=_dirs + (name,),
                )
            await self.cd("..")

    def exit(self):
        """Exit cli"""
        sys.exit()


def main():
    commander = Commander()
    commander.info()
    print(Text("try to type help", style="green"))

    while True:
        try:
            user_input = prompt(
                "pikpak_cli>",
                history=FileHistory("history.txt"),
                auto_suggest=AutoSuggestFromHistory(),
            )
            commander.exec(user_input)
        except KeyboardInterrupt:
            pass
        except EOFError:
            commander.exit()


if __name__ == "__main__":
    main()
