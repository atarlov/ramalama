import json
import os
import subprocess
import sys
import tempfile

from ramalama.model import Model
from ramalama.common import run_cmd, exec_cmd, perror, available, mnt_file

prefix = "oci://"

ocilabeltype = "org.containers.type"
ociimage_raw = "org.containers.type=ai.image.model.raw"
ociimage_car = "org.containers.type=ai.image.model.car"


def list_models(args):
    conman = args.engine
    if conman is None:
        return []

    conman_args = [
        conman,
        "images",
        "--filter",
        f"label={ocilabeltype}",
        "--format",
        '{"name":"oci://{{ .Repository }}:{{ .Tag }}","modified":"{{ .Created }}","size":"{{ .Size }}"},',
    ]
    output = run_cmd(conman_args, debug=args.debug).stdout.decode("utf-8").strip()
    if output == "":
        return []
    return json.loads("[" + output[:-1] + "]")


class OCI(Model):
    def __init__(self, model, conman):
        super().__init__(model.removeprefix(prefix).removeprefix("docker://"))
        self.type = "OCI"
        self.conman = conman
        if available("omlmd"):
            self.omlmd = "omlmd"
        else:
            for i in sys.path:
                self.omlmd = f"{i}/../../../bin/omlmd"
                if os.path.exists(self.omlmd):
                    break
            raise NotImplementedError(
                """\
OCI models requires the omlmd module.
This module can be installed via PyPi tools like pip, pip3, pipx or via
distribution package managers like dnf or apt. Example:
pip install omlmd
"""
            )

    def login(self, args):
        conman_args = [self.conman, "login"]
        if str(args.tlsverify).lower() == "false":
            conman_args.extend([f"--tls-verify={args.tlsverify}"])
        if args.authfile:
            conman_args.extend([f"--authfile={args.authfile}"])
        if args.username:
            conman_args.extend([f"--username={args.username}"])
        if args.password:
            conman_args.extend([f"--password={args.password}"])
        if args.passwordstdin:
            conman_args.append("--password-stdin")
        conman_args.append(args.REGISTRY.removeprefix(prefix))
        return exec_cmd(conman_args, debug=args.debug)

    def logout(self, args):
        conman_args = [self.conman, "logout"]
        conman_args.append(self.model)
        return exec_cmd(conman_args, debug=args.debug)

    def _target_decompose(self, model):
        # Remove the prefix and extract target details
        try:
            registry, reference = model.split("/", 1)
        except Exception:
            raise KeyError(
                f"You must specify a registry for the model in the form "
                f"'oci://registry.acme.org/ns/repo:tag', got instead: {self.model}"
            )

        reference_dir = reference.replace(":", "/")
        return registry, reference, reference_dir

    def _build(self, source, target, args):
        print(f"Building {target}...")
        src = os.path.realpath(source)
        contextdir = os.path.dirname(src)
        model = os.path.basename(src)
        model_name = os.path.basename(source)
        model_raw = f"""\
FROM {args.image} as builder
RUN mkdir -p /models; cd /models; ln -s {model_name} model.file

FROM scratch
COPY --from=builder /models /models
COPY {model} /models/{model_name}
LABEL {ociimage_raw}
"""
        model_car = f"""\
FROM {args.carimage}
RUN mkdir -p /models; cd /models; ln -s {model_name} model.file
COPY {model} /models/{model_name}
LABEL {ociimage_car}
"""

        containerfile = tempfile.NamedTemporaryFile(prefix='RamaLama_Containerfile_', delete=False)
        # Open the file for writing.
        with open(containerfile.name, 'w') as c:
            if args.type == "car":
                c.write(model_car)
            else:
                c.write(model_raw)
        run_cmd(
            [self.conman, "build", "-t", target, "-f", containerfile.name, contextdir], stdout=None, debug=args.debug
        )

    def push(self, source, args):
        target = self.model.removeprefix(prefix)
        source = source.removeprefix(prefix)
        conman_args = [self.conman, "push"]
        if args.authfile:
            conman_args.extend([f"--authfile={args.authfile}"])
        if str(args.tlsverify).lower() == "false":
            conman_args.extend([f"--tls-verify={args.tlsverify}"])

        print(f"Pushing {target}...")
        if source != target:
            try:
                self._build(source, target, args)
                try:
                    conman_args.extend([target])
                    run_cmd(conman_args, debug=args.debug)
                    return
                except subprocess.CalledProcessError as e:
                    perror(f"Failed to push {source} model to OCI: {e}")
                    raise e
            except subprocess.CalledProcessError:
                pass
        try:
            conman_args.extend([source, target])
            run_cmd(conman_args, debug=args.debug)
        except subprocess.CalledProcessError as e:
            perror(f"Failed to push {source} model to OCI {target}: {e}")
            raise e

    def pull(self, args):
        print(f"Downloading {self.model}...")
        if args.engine:
            try:
                conman_args = [args.engine, "pull"]
                if str(args.tlsverify).lower() == "false":
                    conman_args.extend([f"--tls-verify={args.tlsverify}"])
                if args.authfile:
                    conman_args.extend([f"--authfile={args.authfile}"])
                conman_args.extend([self.model])
                run_cmd(conman_args, debug=args.debug)
                return mnt_file
            except subprocess.CalledProcessError:
                pass
        return self._pull_omlmd(args)

    def _pull_omlmd(self, args):
        try:
            registry, reference = self.model.split("/", 1)
        except Exception:
            registry = "docker.io"
            reference = self.model

        reference_dir = reference.replace(":", "/")
        outdir = f"{args.store}/repos/oci/{registry}/{reference_dir}"
        # note: in the current way RamaLama is designed, cannot do Helper(OMLMDRegistry()).pull(target, outdir)
        # since cannot use modules/sdk, can use only cli bindings from pip installs
        run_cmd([self.omlmd, "pull", self.model, "--output", outdir], debug=args.debug)
        ggufs = [file for file in os.listdir(outdir) if file.endswith(".gguf")]
        if len(ggufs) != 1:
            raise KeyError(f"unable to identify .gguf file in: {outdir}")

        directory = f"{args.store}/models/oci/{registry}/{reference_dir}"
        os.makedirs(directory, exist_ok=True)
        model_path = f"{directory}/{ggufs[0]}"
        relative_target_path = os.path.relpath(f"{outdir}/{ggufs[0]}", start=os.path.dirname(model_path))
        if os.path.exists(model_path) and os.readlink(model_path) == relative_target_path:
            # Symlink is already correct, no need to update it
            return model_path

        run_cmd(["ln", "-sf", relative_target_path, model_path], debug=args.debug)
        return model_path

    def model_path(self, args):
        registry, reference = self.model.split("/", 1)
        reference_dir = reference.replace(":", "/")
        path = f"{args.store}/models/oci/{registry}/{reference_dir}"

        if os.path.isfile(path):
            return path

        ggufs = [file for file in os.listdir(path) if file.endswith(".gguf")]
        if len(ggufs) != 1:
            raise KeyError(f"unable to identify .gguf file in: {path}")

        return f"{path}/{ggufs[0]}"

    def remove(self, args):
        try:
            super().remove(args)
        except FileNotFoundError:
            pass

        if self.conman is not None:
            conman_args = [self.conman, "rmi", "--force", self.model]
            exec_cmd(conman_args, debug=args.debug)

    def exists(self, args):
        try:
            model_path = self.model_path(args)
            if os.path.exists(model_path):
                return model_path
        except FileNotFoundError:
            pass

        if self.conman is None:
            return None

        conman_args = [self.conman, "image", "inspect", self.model]
        try:
            run_cmd(conman_args, debug=args.debug)
            return self.model
        except Exception:
            return None
