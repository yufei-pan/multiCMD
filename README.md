# multiCMD

A simple script that is able to issue multiple commands and execute them at the same time locally.

`multiCMD` can display realtime-ish outputs in color if running multiple commands at the same time.

It can be used in bash scripts for automation actions, and it can also be imported and act as a wrapper for `subprocess`.

- Use `return_object=True` with `run_commands` or `run_command` to get the Task Object (definition below).
- Use `quiet=True` and `wait_for_return=False` to create a daemon thread that asynchronously updates the return list / objects when commands finish.

For each process, a thread will be initialized if using `-m/--max_threads > 1`.  
For each thread, `subprocess` is used to open a process for the command task.  
Two additional threads are opened for processing input and output for the task.  

The input / output threads are non-blocking.  
Thus, using `-t/--timeout` will work more reliably.

**Note:** `timeout` specifies how many seconds `multiCMD` will wait before killing the command if **no committed output** was detected for this duration. An output line is considered committed if the **stream handler** encounters a `\n` or `\r` character.


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

options:
  -h, --help            show this help message and exit
  -p, --parse           Parse ranged input and expand them into multiple commands
  -t timeout, --timeout timeout
                        timeout for each command
  -m max_threads, --max_threads max_threads
                        maximum number of threads to use
  -q, --quiet           quiet mode
  -V, --version         show program's version number and exit
```

```python
def run_commands(commands, timeout=0,max_threads=1,quiet=False,dry_run=False,with_stdErr=False,
				 return_code_only=False,return_object=False, parse = False, wait_for_return = True):
	'''
	Run multiple commands in parallel

	@params:
		commands: A list of commands to run ( list[str] | list[list[str]] )
		timeout: The timeout for each command
		max_threads: The maximum number of threads to use
		quiet: Whether to suppress output
		dry_run: Whether to simulate running the commands
		with_stdErr: Whether to append the standard error output to the standard output
		return_code_only: Whether to return only the return code
		return_object: Whether to return the Task object
		parse: Whether to parse ranged input
		wait_for_return: Whether to wait for the return of the commands

	@returns:
		list: The output of the commands ( list[None] | list[int] | list[list[str]] | list[Task] )
  '''
def run_command(command, timeout=0,max_threads=1,quiet=False,dry_run=False,with_stdErr=False,
				return_code_only=False,return_object=False,wait_for_return=True):
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
		wait_for_return: Whether to wait for the return of the command

	@returns:
		None | int | list[str] | Task: The output of the command
	'''
def join_threads(threads=__running_threads,timeout=None):
	'''
	Join threads

	@params:
		threads: The threads to join
		timeout: The timeout

	@returns:
		None
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