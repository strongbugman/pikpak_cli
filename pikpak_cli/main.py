import asyncio
import dataclasses
import json
import os
import signal
import sys
import typing
import functools

from prompt_toolkit import prompt
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.history import FileHistory
from httpx import HTTPError

from pikpak_cli.ant import Pikpak


class CliException(Exception):
    pass


def suppress(func):
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            print(f"Suppress {func.__name__} exception: {type(e)}({e})")
        
    return wrapper


@dataclasses.dataclass
class Session:
    name: str = ".pikpak.session"
    download_dir: str = ""
    current_dirs: typing.List[str] = dataclasses.field(default_factory=lambda: [""])
    current_dir_ids: typing.List[str] = dataclasses.field(default_factory=lambda: [""])
    token: typing.Dict = dataclasses.field(default_factory=dict)

    @property
    def current_dir(self):
        return "/".join(self.current_dirs) + "/"

    @property
    def current_dir_id(self):
        return self.current_dir_ids[-1]

    def load(self):
        if os.path.exists(self.name):
            with open(self.name, "r") as f:
                for k, v in json.load(f).items():
                    setattr(self, k, v)

    def save(self):
        with open(self.name, "w") as f:
            json.dump(dataclasses.asdict(self), f)


@dataclasses.dataclass
class Command:
    call: typing.Callable
    name: str = ""
    help_txt: str = ""

    def __post_init__(self):
        self.name = self.call.__name__
        txt = f"{self.call.__name__}({self.call.__doc__})\n"
        if self.call.__annotations__:
            txt += f"params:\n"
            for n, _ in self.call.__annotations__.items():
                if n.startswith("_"):
                    continue
                txt += f"\t{n}"
        self.help_txt = txt


class Commander:
    def __init__(self) -> None:
        self.session = Session()
        self.session.load()
        self.ant = Pikpak()
        self.ant.auth_pipeline.token = self.session.token
        self.files = {}

        self.CMDS: typing.Dict[str, Command] = {}
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
        ):
            cmd = Command(f)
            self.CMDS[cmd.name] = cmd

    def sizeof_fmt(self, num: int, suffix="B"):
        for unit in ["", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"]:
            if abs(num) < 1024.0:
                return f"{num:3.1f}{unit}{suffix}"
            num /= 1024.0
        return f"{num:.1f}Yi{suffix}"

    def help(self):
        """Get help information"""
        for c in self.CMDS.values():
            print(c.help_txt)
            print("\n")

    def set_download_dir(self, dir: str):
        """Set default download dir"""
        os.makedirs(dir, exist_ok=True)
        self.session.download_dir = dir

    async def login(self, account: str, password: str):
        """Login account"""
        self.session = Session(
            name=self.session.name, download_dir=self.session.download_dir
        )
        token = await self.ant.login(account, password)
        self.session.token = token
        self.session.save()
        print("OK!")

    async def _ls(self):
        self.files = {
            f["name"]: f
            for f in (await self.ant.list_files(parent_id=self.session.current_dir_id))[
                "files"
            ]
        }

    async def ls(self):
        """List current dir files"""
        if not self.files:
            await self._ls()
        print(
            "\n".join(
                f["modified_time"]
                + " "
                + self.sizeof_fmt(int(f["size"]))
                + " "
                + f["name"]
                + ("/" if "folder" in f["kind"] else "")
                for f in self.files.values()
            )
        )

    def pwd(self):
        """Get current path"""
        print(self.session.current_dir)

    async def cd(self, *name: str):
        """Change directory"""
        if not self.files:
            await self._ls()

        name = " ".join(name)

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
            print("wrong name!")
            return
        if "folder" not in self.files[name]["kind"]:
            print("not a folder!")
            return

        self.session.current_dir_ids.append(self.files[name]["id"])
        self.session.current_dirs.append(self.files[name]["name"])
        self.session.save()
        await self._ls()

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

    async def du(self, *name):
        """Get files's total size"""
        name = " ".join(name)
        if name:
            await self.cd(name)

        size = 0
        async for f in self.traverse_files():
            size += int(f["size"])

        print("all size: ", self.sizeof_fmt(size))

        if name:
            await self.cd("..")

    @suppress
    async def download(self, *name: str, _dirs: typing.Sequence[str] = tuple()):
        """Download one file or all files in one directory"""
        name = " ".join(name)
        if name == ".":
            file = {"kind": "folder", "id": self.session.current_dir_id, "name": "."}
        else:
            file = self.files.get(name, {})
        if not file:
            print("wrong file name!")
            return
        size = 100 * 1024 * 1024
        size = 1024

        if "file" in file["kind"]:
            try:
                data = await self.ant.get_file_link(self.files[name]["id"])
            except HTTPError as e:
                if "unauthenticated" in str(e):
                    await self.login("", "")
                    data = await self.ant.get_file_link(self.files[name]["id"])
                else:
                    raise e

            if int(data["size"]) < size:
                return
            filename = os.path.join(self.session.download_dir, *_dirs, name)
            os.makedirs(os.path.dirname(filename), exist_ok=True)
            print(f"Downloading {name} to {filename}...")
            await self.ant.download(
                data["links"]["application/octet-stream"]["url"], filename
            )
            await asyncio.sleep(0.5)
        else:
            await self.cd(name)
            for f in self.files.values():
                await self.download(f["name"], _dirs=_dirs + (name,))
            await self.cd("..")

    def exit(self):
        sys.exit()

    def exec(self, input: str):
        if not input:
            return

        args = input.split(" ")
        if args[0] not in self.CMDS:
            print("Wrong command!")
            return

        try:
            res = self.CMDS[args[0]].call(*args[1:])
            if asyncio.iscoroutine(res):
                asyncio.get_event_loop().run_until_complete(res)
        except CliException as e:
            print(type(e), e)
        except KeyboardInterrupt:
            pass


commander = Commander()
signal.signal(signal.SIGINT, commander.exit)
signal.signal(signal.SIGTERM, commander.exit)

def main():
    while True:
        print("try to type help")
        user_input = prompt(
            "pikpak_cli>",
            history=FileHistory("history.txt"),
            auto_suggest=AutoSuggestFromHistory(),
        )
        commander.exec(user_input)

if __name__ == "__main__":
    main()
