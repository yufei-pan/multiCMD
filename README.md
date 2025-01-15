# multiCMD
A simple script that is able to issue multiple commands and execute them at the same time locally.

multiCMD can display realtime-ish outputs in color if running multiple commands at the same time.

Can be used in bash scripts for automation actions.

Also able to be imported and act as a wrapper for subprocess.

For each process, it will initialize a thread if using -m/--max_threads > 1

For each thread, it will use subprocess lib to open a process for the command task

And it will open two sub threads for processing input and output for the task.

They input / output threads will be non-blocking.

Thus using -t/--timeout will work more reliably.

Note: timeout specifies how many seconds multiCMD will kill the command if NO COMMITTED OUTPUT was detected from the program for this long.

An output line is considered committed if steram hanlder encounters a '\n' or '\r' character.


Install via
```bash
pip install multiCMD
```

multiCMD will be available as
```bash
mcmd
multiCMD
multicmd
```


```bash
$ mcmd -h
usage: mcmd [-h] [-t timeout] [-m max_threads] [-q] [-V] command [command ...]

Run multiple commands in parallel

positional arguments:
  command               commands to run

optional arguments:
  -h, --help            show this help message and exit
  -t timeout, --timeout timeout
                        timeout for each command
  -m max_threads, --max_threads max_threads
                        maximum number of threads to use
  -q, --quiet           quiet mode
  -V, --version         show program's version number and exit
```

```python
def run_commands(commands, timeout=0,max_threads=1,quiet=False,dry_run=False,with_stdErr=False,
				 return_code_only=False,return_object=False):
	'''
	Run multiple commands in parallel

	@params:
		commands: A list of commands to run
		timeout: The timeout for each command
		max_threads: The maximum number of threads to use
		quiet: Whether to suppress output
		dry_run: Whether to simulate running the commands
		with_stdErr: Whether to append the standard error output to the standard output
		return_code_only: Whether to return only the return code
		return_object: Whether to return the Task object

	@returns:
		list: The output of the commands ( list[None] | list[int] | list[list[str]] | list[Task] )
	'''
def run_command(command, timeout=0,max_threads=1,quiet=False,dry_run=False,with_stdErr=False,
				return_code_only=False,return_object=False):
	'''
	Run a command

	@params:
		command: The command to run
		timeout: The timeout for the command
		max_threads: The maximum number of threads to use
		quiet: Whether to suppress output
		dry_run: Whether to simulate running the command
		with_stdErr: Whether to append the standard error output to the standard output
		return_code_only: Whether to return only the return code
		return_object: Whether to return the Task object

	@returns:
		None | int | list[str] | Task: The output of the command
	'''
def input_with_timeout_and_countdown(timeout, prompt='Please enter your selection'):
	"""
	Read an input from the user with a timeout and a countdown.

	@params:
		timeout: The timeout in seconds
		prompt: The prompt to display to the user

	@returns:
		str: The input from the user or None if no input was received

	"""
def print_progress_bar(iteration, total, prefix='', suffix=''):
	'''
	Call in a loop to create terminal progress bar
	@params:
		iteration   - Required  : current iteration (Int)
		total       - Required  : total iterations (Int)
		prefix      - Optional  : prefix string (Str)
		suffix      - Optional  : suffix string (Str)

	@returns:
		None
	'''
def get_terminal_size():
	'''
	Get the terminal size

	@params:
		None

	@returns:
		(int,int): the number of columns and rows of the terminal
	'''
def int_to_color(n, brightness_threshold=500):
	'''
	Convert an integer to a color

	@params:
		n: The integer
		brightness_threshold: The brightness threshold

	@returns:
		(int,int,int): The RGB color
	'''
class Task:
	def __init__(self, command):
		self.command = command
		self.returncode = None
		self.stdout = []
		self.stderr = []
```