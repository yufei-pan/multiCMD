#!/usr/bin/env python3
import time
import threading
import io
import argparse
import sys
import subprocess
import select
import os

version = '1.13'
__version__ = version

class Task:
	def __init__(self, command):
		self.command = command
		self.returncode = None
		self.stdout = []
		self.stderr = []
	def __iter__(self):
		return zip(['command', 'returncode', 'stdout', 'stderr'], [self.name, self.command, self.returncode, self.stdout, self.stderr])

def __handle_stream(stream,target,pre='',post='',quiet=False):
	'''
	Handle a stream

	@params:
		stream: The stream to handle
		target: The target to write to
		pre: The prefix to add to each line
		post: The postfix to add to each line
		quiet: Whether to suppress output

	@returns:
		None
	'''
	def add_line(current_line,target, keepLastLine=True):
		if not keepLastLine:
			if not quiet:
				sys.stdout.write('\r')
			target.pop()
		else:
			if not quiet:
				sys.stdout.write('\n')
		current_line_str = current_line.decode('utf-8',errors='backslashreplace')
		target.append(current_line_str)
		if not quiet:
			sys.stdout.write(pre+current_line_str+post)
			sys.stdout.flush()
	current_line = bytearray()
	lastLineCommited = True
	for char in iter(lambda:stream.read(1), b''):
		if char == b'\n':
			if (not lastLineCommited) and current_line:
				add_line(current_line,target, keepLastLine=False)
			elif lastLineCommited:
				add_line(current_line,target, keepLastLine=True)
			current_line = bytearray()
			lastLineCommited = True
		elif char == b'\r':
			add_line(current_line,target, keepLastLine=lastLineCommited)
			current_line = bytearray()
			lastLineCommited = False
		else:
			current_line.extend(char)
	if current_line:
		add_line(current_line,target, keepLastLine=lastLineCommited)

def int_to_color(n, brightness_threshold=500):
	'''
	Convert an integer to a color

	@params:
		n: The integer
		brightness_threshold: The brightness threshold

	@returns:
		(int,int,int): The RGB color
	'''
	hash_value = hash(str(n))
	r = (hash_value >> 16) & 0xFF
	g = (hash_value >> 8) & 0xFF
	b = hash_value & 0xFF
	if (r + g + b) < brightness_threshold:
		return int_to_color(hash_value, brightness_threshold)
	return (r, g, b)

def __run_command(task,sem, timeout=60, quiet=False,dry_run=False,with_stdErr=False,identity=None):
	'''
	Run a command ( internal )

	@params:
		task: The Task object
		sem: The semaphore
		timeout: The timeout for the command
		quiet: Whether to suppress output
		dry_run: Whether to simulate running the command
		with_stdErr: Whether to return the standard error output
		identity: The identity of the command

	@returns:
		None | int | list[str]: The output of the command
	'''

	pre = ''
	post = ''
	with sem:
		try:
			if identity is not None:
				if identity == ...:
					identity = threading.get_ident()
				r, g, b = int_to_color(identity)
				pre = f'\033[38;2;{r};{g};{b}m'
				post = '\033[0m'
			if not quiet:
				print(pre+'Running command: '+' '.join(task.command)+post)
				print(pre+'-'*100+post)
			if dry_run:
				return task.stdout + task.stderr
			#host.stdout = []
			proc = subprocess.Popen(task.command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,stdin=subprocess.PIPE)
			# create a thread to handle stdout
			stdout_thread = threading.Thread(target=__handle_stream, args=(proc.stdout,task.stdout,pre,post,quiet))
			stdout_thread.start()
			# create a thread to handle stderr
			#host.stderr = []
			stderr_thread = threading.Thread(target=__handle_stream, args=(proc.stderr,task.stderr,pre,post,quiet))
			stderr_thread.start()
			# Monitor the subprocess and terminate it after the timeout
			start_time = time.time()
			outLength = len(task.stdout) + len(task.stderr)
			while proc.poll() is None:  # while the process is still running
				if len(task.stdout) + len(task.stderr) != outLength:
					start_time = time.time()
					outLength = len(task.stdout) + len(task.stderr)
				if timeout > 0 and time.time() - start_time > timeout:
					task.stderr.append('Timeout!')
					proc.terminate()
					break
				time.sleep(0.1)  # avoid busy-waiting
			task.returncode = proc.poll()
			# Wait for output processing to complete
			stdout_thread.join(timeout=1)
			stderr_thread.join(timeout=1)
			# here we handle the rest of the stdout after the subprocess returns
			stdout, stderr = proc.communicate()
			if stdout:
				__handle_stream(io.BytesIO(stdout),task.stdout, task)
			if stderr:
				__handle_stream(io.BytesIO(stderr),task.stderr, task)
			task.returncode = proc.poll()
			if not quiet:
				print(pre+'\n'+ '-'*100+post)
				print(pre+f'Process exited with return code {task.returncode}'+post)
		except Exception as e:
			import traceback
			task.stderr.extend(str(e).split('\n'))
			task.stderr.extend(traceback.format_exc().split('\n'))
			task.returncode = -1
		if with_stdErr:
			return task.stdout + task.stderr
		else:
			return task.stdout

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
		with_stdErr: Whether to return the standard error output
		return_code_only: Whether to return only the return code
		return_object: Whether to return the Task object

	@returns:
		None | int | list[str] | Task: The output of the command
	'''
	return run_commands([command], timeout, max_threads, quiet, dry_run, with_stdErr, return_code_only, return_object)[0]

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
		with_stdErr: Whether to return the standard error output
		return_code_only: Whether to return only the return code
		return_object: Whether to return the Task object

	@returns:
		list: The output of the commands ( list[None] | list[int] | list[list[str]] | list[Task] )
	'''
	# split the commands in commands if it is a string
	commands = [command.split() if isinstance(command,str) else command for command in commands]
	# initialize the tasks
	tasks = [Task(command) for command in commands]
	# run the tasks with max_threads. if max_threads is 0, use the number of commands
	if max_threads < 1:
		max_threads = len(commands)
	if max_threads > 1:
		sem = threading.Semaphore(max_threads)  # Limit concurrent sessions
		threads = [threading.Thread(target=__run_command, args=(task,sem,timeout,quiet,dry_run,...)) for task in tasks]
		for thread in threads:
			thread.start()
		for thread in threads:
			thread.join()
	else:
		# just process the commands sequentially
		sem = threading.Semaphore(1)
		for task in tasks:
			__run_command(task,sem,timeout,quiet,dry_run,identity=None)
	# return the only the output for the tasks
	if return_code_only:
		return [task.returncode for task in tasks]
	elif return_object:
		return tasks
	elif with_stdErr:
		return [task.stdout + task.stderr for task in tasks]
	else:
		return [task.stdout for task in tasks]

def input_with_timeout_and_countdown(timeout, prompt='Please enter your selection'):
	"""
	Read an input from the user with a timeout and a countdown.

	@params:
		timeout: The timeout in seconds
		prompt: The prompt to display to the user

	@returns:
		str: The input from the user or None if no input was received

	"""
	# Print the initial prompt with the countdown
	print(f"{prompt} [{timeout}s]: ", end='', flush=True)
	# Loop until the timeout
	for remaining in range(timeout, 0, -1):
		# If there is an input, return it
		if sys.stdin in select.select([sys.stdin], [], [], 0)[0]:
			return input().strip()
		# Print the remaining time
		print(f"\r{prompt} [{remaining}s]: ", end='', flush=True)
		# Wait a second
		time.sleep(1)
	# If there is no input, return None
	return None

def _genrate_progress_bar(iteration, total, prefix='', suffix='',columns=120):
	'''
	Generate a progress bar string

	@params:
		iteration   - Required  : current iteration (Int)
		total       - Required  : total iterations (Int)
		prefix      - Optional  : prefix string (Str)
		suffix      - Optional  : suffix string (Str)
		columns     - Optional  : number of columns in the terminal (Int)

	@returns:
		str: the progress bar string
	'''
	noPrefix = False
	noSuffix = False
	noPercent = False
	noBar = False
	# if total is 0, we don't want to divide by 0
	if total == 0:
		return f'{prefix} iteration:{iteration} {suffix}'.ljust(columns)
	percent = f'|{("{0:.1f}").format(100 * (iteration / float(total)))}% '
	length = columns - len(prefix) - len(suffix) - len(percent) - 3
	if length <= 0:
		length = columns - len(prefix) - len(suffix) - 3
		noPercent = True
	if length <= 0:
		length = columns - len(suffix) - 3
		noPrefix = True
	if length <= 0:
		length = columns - 3
		noSuffix = True
	if length <= 0:
		return f'{prefix}\niteration:\n {iteration}\ntotal:\n {total}\n| {suffix}\n'
	if iteration == 0:
		noBar = True
	filled_length = int(length * iteration // total)
	progress_chars = '▁▂▃▄▅▆▇█'
	fractional_progress = (length * iteration / total) - filled_length
	char_index = int(fractional_progress * (len(progress_chars) - 1))
	bar_char = progress_chars[char_index]
	if filled_length == length:
		bar = progress_chars[-1] * length
	else:
		bar = progress_chars[-1] * filled_length + bar_char + '_' * (length - filled_length)
	lineOut = ''
	if not noPrefix:
		lineOut += prefix
	if not noBar:
		lineOut += f'{bar}'
		if not noPercent:
			lineOut += percent
	else:
		if length >= 16:
			lineOut += f' Calculating... '
	if not noSuffix:
		lineOut += suffix
	return lineOut

def get_terminal_size():
	'''
	Get the terminal size

	@params:
		None

	@returns:
		(int,int): the number of columns and rows of the terminal
	'''
	try:
		import os
		_tsize = os.get_terminal_size()
	except:
		try:
			import fcntl, termios, struct
			packed = fcntl.ioctl(0, termios.TIOCGWINSZ, struct.pack('HHHH', 0, 0, 0, 0))
			_tsize = struct.unpack('HHHH', packed)[:2]
		except:
			import shutil
			_tsize = shutil.get_terminal_size(fallback=(120, 30))
	return _tsize

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
	prefix += ' |' if not prefix.endswith(' |') else ''
	suffix = f'| {suffix}' if not suffix.startswith('| ') else suffix
	try:
		columns, _ = get_terminal_size()
		sys.stdout.write(f'\r{_genrate_progress_bar(iteration, total, prefix, suffix, columns)}')
		sys.stdout.flush()
		if iteration == total and total > 0:
			print(file=sys.stdout)
	except:
		if iteration % 5 == 0:
			print(_genrate_progress_bar(iteration, total, prefix, suffix))


def main():
	parser = argparse.ArgumentParser(description='Run multiple commands in parallel')
	parser.add_argument('commands', metavar='command', type=str, nargs='+',help='commands to run')
#	parser.add_argument('-p','--parse', action='store_true',help='parse ranged input')
	parser.add_argument('-t','--timeout', metavar='timeout', type=int, default=60,help='timeout for each command')
	parser.add_argument('-m','--max_threads', metavar='max_threads', type=int, default=1,help='maximum number of threads to use')
	parser.add_argument('-q','--quiet', action='store_true',help='quiet mode')
	parser.add_argument('-V','--version', action='version', version=f'%(prog)s {version} by pan@zopyr.us')
	args = parser.parse_args()
	run_commands(args.commands, args.timeout, args.max_threads, args.quiet)

if __name__ == '__main__':
	main()