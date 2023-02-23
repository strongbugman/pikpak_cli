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

import IPython
import tenacity
from httpx import HTTPError
from prompt_toolkit import prompt
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.history import FileHistory
from rich import print, tree
from rich.text import Text

from pikpak_cli.ant import Pikpak


class CliException(Exception):
    pass


@dataclasses.dataclass
class Session:
    name: str = ".pikpak.session"
    download_dir: str = "./"
    token: typing.Dict = dataclasses.field(default_factory=dict)
    account: str = ""
    password: str = ""

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
class File:
    source_data: typing.Dict[str, str]
    childrens: typing.Dict[str, "File"] = dataclasses.field(default_factory=dict)
    father: typing.Optional["File"] = None

    @staticmethod
    def size2str(num: int, suffix="B") -> str:
        for unit in ["", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"]:
            if abs(num) < 1024.0:
                return f"{num:3.1f}{unit}{suffix}"
            num /= 1024.0
        return f"{num:.1f}Yi{suffix}"

    @staticmethod
    def size2int(size: str) -> int:
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

    @property
    def name(self) -> str:
        return self.source_data.get("name")

    @property
    def size(self) -> int:
        return int(self.source_data.get("size", 0))

    @property
    def human_size(self) -> str:
        return self.size2str(self.size)

    @property
    def id(self) -> str:
        return self.source_data.get("id")

    @property
    def path(self) -> str:
        return f"{self.father.path}/{self.name}" if self.father else self.name

    @property
    def dirs(self) -> typing.List[str]:
        return self.father.dirs + [self.father.name] if self.father else []

    @property
    def description(self) -> str:
        return (
            Text(self.source_data.get("modified_time", ""), style="gray")
            + " "
            + Text(self.human_size, style="blue")
            + " "
            + Text(self.name, style="green")
            + ("/" if self.is_floder else "")
        )

    @property
    def is_floder(self) -> bool:
        return "folder" in self.source_data.get("kind")


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
        self.root_file = File({"kind": "folder", "name": "", id: ""})
        self.current_file = self.root_file
        self.CMDS: typing.Dict[str, Command] = {}
        asyncio.ensure_future(self.refresh_token())

        for f in (
            self.login,
            self.exit,
            self.shell,
            self.ls,
            self.cd,
            self.download,
            self.pwd,
            self.du,
            self.help,
            self.config,
            self.info,
            self.rm,
        ):
            cmd = Command(f)
            self.CMDS[cmd.name] = cmd

    async def fetch_file_childen(self, file: File) -> typing.Dict[str, File]:
        if not file.childrens:
            file.childrens = {
                f["name"]: File(f, father=file)
                for f in (await self.ant.list_files(parent_id=file.id))["files"]
            }
        return file.childrens

    async def find_file(self, file: File, name: str) -> File:
        for n in name.split("/"):
            if n == "/":
                file = self.root_file
            elif n == ".":
                continue
            elif n == "..":
                file = file.father if file.father else file
            elif n not in await self.fetch_file_childen(file):
                raise CliException(f"{name} not found")
            else:
                file = file.childrens[n]

        return file

    async def traverse_files(
        self, file: File, recursion: bool = True
    ) -> typing.AsyncGenerator[File, None]:
        if not file.is_floder:
            yield file
            return

        for f in (await self.fetch_file_childen(file)).values():
            yield f
            if f.is_floder and recursion:
                async for _f in self.traverse_files(f):
                    yield _f

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

    def shell(self):
        IPython.embed(header=f"Shell:\n", using="asyncio")

    def info(self):
        """Print session info"""
        print(Text(f"Current account: {self.session.account}", style="blue"))
        print(Text(f"Default download dir: {self.session.download_dir}", style="blue"))
        print(Text(f"session file: {self.session.name}", style="blue"))

    def help(self):
        """Get help information"""
        for c in self.CMDS.values():
            print(c.help_text)

    def config(self, downlaod_dir: str = ""):
        """Set default download dir or"""
        os.makedirs(downlaod_dir, exist_ok=True)
        self.session.download_dir = downlaod_dir

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

    async def ls(
        self,
        name: str,
        without_audit: bool = False,
        trash: bool = False,
        recursion: bool = False,
    ):
        """List current dir files"""
        file = await self.find_file(self.current_file, name)
        root_tree = tree.Tree(file.description)
        trees = {file.id: root_tree}
        async for f in self.traverse_files(file, recursion=recursion):
            if f.source_data.get("trashed") and not trash:
                continue
            if f.source_data.get("audit") and without_audit:
                continue
            t = trees[f.father.id]
            trees[f.id] = t.add(f.description)

        print(root_tree)

    def pwd(self):
        """Get current path"""
        print(Text(self.current_file.path, style="green"))

    async def cd(self, name: str):
        """Change directory"""
        file = await self.find_file(self.current_file, name)
        if not file.is_floder:
            raise CliException(f"{name} is not a floder")

        self.current_file = file

    async def du(self, name: str):
        """Get files's total size"""
        file = await self.find_file(self.current_file, name)

        size = 0
        async for f in self.traverse_files(file):
            size += f.size

        print(Text("all size: ", "green"), Text(File.size2str(size), "blue"))

    async def rm(self, name: str, no_trash: bool = False):
        file = await self.find_file(self.current_file, name)

        await self.ant.delete_file([file.id], trash=not no_trash)
        file.father.childrens.pop(file.name)

    async def download(
        self,
        name: str,
        includes: str = "",
        excludes: str = "",
        dir: str = "",
        size: str = "",
        relative_path: bool = False,
        new_file_name: str = "",
    ):
        """Download a file or many files in a directory"""
        file = await self.find_file(self.current_file, name)
        async for f in self.traverse_files(file):
            if f.is_floder:
                continue
            if f.source_data.get("trashed"):
                continue
            # filter
            if includes:
                include = False
                for p in includes.split(","):
                    if fnmatch.fnmatch(f.name, p):
                        include = True
                        break
                if not include:
                    print(
                        Text(
                            f"{f.name} ignored by pattern {p} not matched",
                            style="green",
                        )
                    )
                    continue
            if excludes:
                exclude = False
                for p in excludes.split(","):
                    if fnmatch.fnmatch(f.name, p):
                        exclude = True
                        break
                if exclude:
                    print(
                        Text(
                            f"{f.name} ignored by pattern {p} matched",
                            style="green",
                        )
                    )
                    continue
            if size and f.size < File.size2int(size):
                print(
                    Text(
                        f"{f.name}({f.human_size}) ignored by size limit",
                        style="green",
                    )
                )
                continue
            data = await self.ant.get_file_link(f.id)
            # download
            if not dir:
                dir = self.session.download_dir
            _file_name = new_file_name
            if not _file_name:
                _file_name = f.name
            if relative_path:
                filename = os.path.join(dir, _file_name)
            else:
                filename = os.path.join(
                    dir, *f.dirs[len(self.current_file.dirs) + 1 :], _file_name
                )
            os.makedirs(os.path.dirname(filename), exist_ok=True)
            print(Text(f"Downloading {f.name} to {filename}...", style="green"))
            await tenacity.retry(
                wait=tenacity.wait_fixed(60),
                stop=tenacity.stop_after_attempt(3 + 1),
                retry=tenacity.retry_if_exception_type(HTTPError),
                reraise=True,
            )(self.ant.download)(
                data["links"]["application/octet-stream"]["url"], filename
            )
            print(Text(f"Downloaded {filename}", style="blue"))
            await asyncio.sleep(0.5)

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
