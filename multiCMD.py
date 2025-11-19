#!/usr/bin/env python3
# /// script
# requires-python = ">=3.6"
# dependencies = [
#     "argparse",
# ]
# ///
#%% imports
import argparse
import io
import itertools
import math
import re
import select
import signal
import string
import subprocess
import sys
import threading
import time

#%% global vars
version = '1.40'
__version__ = version
COMMIT_DATE = '2025-11-18'
__running_threads = set()
__variables = {}

# immutable helpers compiled once at import time
_BRACKET_RX   = re.compile(r'\[([^\]]+)\]')
_ALPHANUM     = string.digits + string.ascii_letters
_ALPHA_IDX    = {c: i for i, c in enumerate(_ALPHANUM)}


#%% objects
class Task:
	def __init__(self, command):
		self.command = command
		self.returncode = None
		self.stdout = []
		self.stderr = []
		self.thread = None
		self.stop = False
	def __iter__(self):
		return zip(['command', 'returncode', 'stdout', 'stderr'], [self.command, self.returncode, self.stdout, self.stderr])
	def __repr__(self):
		return f'Task(command={self.command}, returncode={self.returncode}, stdout={self.stdout}, stderr={self.stderr}, stop={self.stop})'
	def __str__(self):
		return str(dict(self))
	def is_alive(self):
		if self.thread is not None:
			return self.thread.is_alive()
		return False

class AsyncExecutor:
	def __init__(self, max_threads=1,semaphore=...,timeout=0,quiet=True,dry_run=False,parse=False):
		'''
		AsyncExecutor class to run commands in parallel asynchronously
		@params:
			max_threads: The maximum number of threads to use ( int ) ( Note: if passing semaphore, this likely will be ignored )
			semaphore: The semaphore to use for threading ( threading.Semaphore )
			timeout: The timeout for each command ( int )
			quiet: Whether to suppress output ( bool )
			dry_run: Whether to simulate running the commands ( bool )
			parse: Whether to parse ranged input ( bool )
		'''
		self.max_threads = max_threads
		if semaphore is ...:
			semaphore = threading.Semaphore(max_threads)
		self.semaphore = semaphore
		self.runningThreads = []
		self.tasks = []
		self.timeout = timeout
		self.quiet = quiet
		self.dry_run = dry_run
		self.parse = parse
		self.__lastNotJoined = 0

	def __iter__(self):
		return iter(self.tasks)
	
	def __repr__(self):
		return f'AsyncExecutor(max_threads={self.max_threads}, semaphore={self.semaphore}, runningThreads={self.runningThreads}, tasks={self.tasks}, timeout={self.timeout}, quiet={self.quiet}, dry_run={self.dry_run}, parse={self.parse})'
	
	def __str__(self):
		return str(self.tasks)
	
	def __len__(self):
		return len(self.tasks)
	
	def __bool__(self):
		return bool(self.tasks)
	
	def run_commands(self, commands, timeout=...,max_threads=...,quiet=...,dry_run=...,parse = ...,sem = ...):
		'''
		Run multiple commands in parallel asynchronously
		@params:
			commands: A list of commands to run ( list[str] | list[list[str]] )
			timeout: The timeout for each command to override the object default
			max_threads: The maximum number of threads to use to override the object default
			quiet: Whether to suppress output to override the object default
			dry_run: Whether to simulate running the commands to override the object default
			parse: Whether to parse ranged input to override the object default
			sem: The semaphore to use for threading to override the object default
		@returns:
			list: The Task Object of the commands ( list[Task] )
		'''
		if timeout is ...:
			timeout = self.timeout
		if max_threads is ...:
			max_threads = self.max_threads
		if quiet is ...:
			quiet = self.quiet
		if dry_run is ...:
			dry_run = self.dry_run
		if parse is ...:
			parse = self.parse
		if sem is ...:
			sem = self.semaphore
		if len(self.runningThreads) > 130000:
			self.wait(timeout=0)
			if len(self.runningThreads) > 130000:
				print('The amount of running threads approching cpython limit of 130704. Waiting until some available.')
				while len(self.runningThreads) > 120000:
					self.wait(timeout=1)
		elif len(self.runningThreads) + self.__lastNotJoined > 1000:
			self.wait(timeout=0)
			self.__lastNotJoined = len(self.runningThreads)
		taskObjects: list[Task] = run_commands(commands,timeout=timeout,max_threads=max_threads,quiet=quiet,dry_run=dry_run,with_stdErr=False,
				 return_code_only=False,return_object=True, parse = parse, wait_for_return = False, sem = sem)
		self.tasks.extend(taskObjects)
		self.runningThreads.extend([task.thread for task in taskObjects])
		return taskObjects
	
	def run_command(self, command, timeout=...,max_threads=...,quiet=...,dry_run=...,parse = ...,sem = ...):
		'''
		Run a command in parallel asynchronously
		@params:
			command: The command to run ( str | list[str] )
			timeout: The timeout for each command to override the object default
			max_threads: The maximum number of threads to use to override the object default
			quiet: Whether to suppress output to override the object default
			dry_run: Whether to simulate running the commands to override the object default
			parse: Whether to parse ranged input to override the object default
			sem: The semaphore to use for threading to override the object default
		@returns:
			Task: The Task Object of the command
		'''
		return self.run_commands([command],timeout=timeout,max_threads=max_threads,quiet=quiet,dry_run=dry_run,parse=parse,sem=sem)[0]
	
	def wait(self, timeout=..., threads = ...):
		'''
		Wait for the threads to finish
		@params:
			timeout: The timeout for each command to override the object default
			threads: The threads to join, default to all running threads managed by this object
		@returns:
			list[threading.Thread]: The list of running threads that are still running
		'''
		if threads is ...:
			threads = self.runningThreads
		if timeout is ...:
			timeout = self.timeout
		for thread in threads:
			if timeout >= 0:
				thread.join(timeout=timeout)
			else:
				thread.join()
		self.runningThreads = [thread for thread in self.runningThreads if thread.is_alive()]
		return self.runningThreads
	
	def stop(self,timeout=...):
		'''
		Stop all running threads. This signals all threads to stop and joins them
		@params:
			None
		@returns:
			list[Task]: The list of tasks that are managed by this object
		'''
		for task in self.tasks:
			task.stop = True
		self.wait(timeout)
		return self.tasks
	
	def cleanup(self,timeout=...):
		'''
		Cleanup the tasks and threads. This calls stop() and clears the tasks and threads
		@params:
			None
		@returns:
			list[Task]: The list of tasks that are managed by this object
		'''
		self.stop(timeout)
		self.tasks = []
		self.runningThreads = []
		return self.tasks
	
	def join(self, timeout=..., threads = ..., print_error=True):
		'''
		Wait for the threads to finish and print error if there is any
		@params:
			timeout: The timeout for each command to override the object default
			threads: The threads to wait for, default to all running threads managed by this object
		@returns:
			list[Task]: The list of tasks that are managed by this object
		'''
		self.wait(timeout=timeout, threads=threads)
		for task in self.tasks:
			if task.returncode != 0 and print_error:
				print(f'Command: {task.command} failed with return code: {task.returncode}')
				print('Stdout:')
				print('\n  '.join(task.stdout))
				print('Stderr:')
				print('\n  '.join(task.stderr))
		return self.tasks
	
	def get_results(self, with_stdErr=False):
		'''
		Get the results of the tasks
		@params:
			with_stdErr: Whether to append the standard error output to the standard output	
		@returns:
			list[list[str]]: The output of the tasks ( list[list[str]] )
		'''
		if with_stdErr:
			return [task.stdout + task.stderr for task in self.tasks]
		else:
			return [task.stdout for task in self.tasks]
		
	def get_return_codes(self):
		'''
		Get the return codes of the tasks
		@params:
			None
		@returns:
			list[int]: The return codes of the tasks ( list[int] )
		'''
		return [task.returncode for task in self.tasks]

#%% helper functions
def _expand_piece(piece, vars_):
	"""Turn one comma-separated element from inside [...] into a list of strings."""
	piece = piece.strip()

	# variable assignment  foo:BAR
	if ':' in piece:
		var, _, value = piece.partition(':')
		vars_[var] = value
		return                            # bracket disappears

	# explicit range  start-end
	if '-' in piece:
		start, _, end = (p.strip() for p in piece.partition('-'))

		start = vars_.get(start, start)
		end   = vars_.get(end,   end)

		if start.isdigit() and end.isdigit():               # decimal range
			pad = max(len(start), len(end))
			return [f"{i:0{pad}d}" for i in range(int(start), int(end) + 1)]

		if all(c in string.hexdigits for c in start + end): # hex range
			return [format(i, 'x') for i in range(int(start, 16),
												  int(end,   16) + 1)]

		# alphanumeric range (0-9a-zA-Z)
		try:
			return [_ALPHANUM[i]
					for i in range(_ALPHA_IDX[start], _ALPHA_IDX[end] + 1)]
		except KeyError:
			pass                                            # fall through

	# plain token or ${var}
	return [vars_.get(piece, piece)]

def _expand_ranges_fast(inStr):
	global __variables
	segments: list[list[str]] = []
	pos = 0
	# split the template into literal pieces + expandable pieces
	for m in _BRACKET_RX.finditer(inStr):
		if m.start() > pos:
			segments.append([inStr[pos:m.start()]])          # literal
		choices: list[str] = []
		for sub in m.group(1).split(','):
			expandedPieces = _expand_piece(sub, __variables)
			if expandedPieces:
				choices.extend(expandedPieces)
		segments.append(choices or [''])                        # keep length
		pos = m.end()
	segments.append([inStr[pos:]])                           # tail

	# cartesian product of all segments
	return [''.join(parts) for parts in itertools.product(*segments)]

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

def int_to_color(hash_value, min_brightness=100,max_brightness=220):
	r = (hash_value >> 16) & 0xFF
	g = (hash_value >> 8) & 0xFF
	b = hash_value & 0xFF
	brightness = math.sqrt(0.299 * r**2 + 0.587 * g**2 + 0.114 * b**2)
	if brightness < min_brightness:
		return int_to_color(hash(str(hash_value)), min_brightness, max_brightness)
	if brightness > max_brightness:
		return int_to_color(hash(str(hash_value)), min_brightness, max_brightness)
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
				pre = f'\x1b[38;2;{r};{g};{b}m'
				post = '\x1b[0m'
			if not quiet:
				print(pre+'Running command: '+' '.join(task.command)+post)
				print(pre+'-'*100+post)
			if dry_run:
				return task.stdout + task.stderr
			#host.stdout = []
			proc = subprocess.Popen(task.command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,stdin=subprocess.PIPE)
			# create a thread to handle stdout
			stdout_thread = threading.Thread(target=__handle_stream, args=(proc.stdout,task.stdout,pre,post,quiet),daemon=True)
			stdout_thread.start()
			# create a thread to handle stderr
			#host.stderr = []
			stderr_thread = threading.Thread(target=__handle_stream, args=(proc.stderr,task.stderr,pre,post,quiet),daemon=True)
			stderr_thread.start()
			# Monitor the subprocess and terminate it after the timeout
			start_time = time.time()
			outLength = len(task.stdout) + len(task.stderr)
			time.sleep(0)
			sleep_time = 1.0e-7 # 100 nanoseconds
			while proc.poll() is None:  # while the process is still running
				if task.stop:
					proc.send_signal(signal.SIGINT)
					time.sleep(0.01)
					proc.terminate()
					break
				if timeout > 0:
					if len(task.stdout) + len(task.stderr) != outLength:
						start_time = time.time()
						outLength = len(task.stdout) + len(task.stderr)
					elif time.time() - start_time > timeout:
						task.stderr.append('Timeout!')
						proc.send_signal(signal.SIGINT)
						time.sleep(0.01)
						proc.terminate()
						break
				time.sleep(sleep_time)
				# exponential backoff
				if sleep_time < 0.001:
					sleep_time *= 2
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
			if task.returncode is None:
				# process been killed via timeout or sigkill
				if task.stderr and task.stderr[-1].strip().startswith('Timeout!'):
					task.returncode = 124
				elif task.stderr and task.stderr[-1].strip().startswith('Ctrl C detected, Emergency Stop!'):
					task.returncode = 137
				else:
					task.returncode = -1
		# if file not found
		except FileNotFoundError as e:
			print(f'Command path not found: {task.command[0]}',file=sys.stderr,flush=True)
			task.stderr.append(str(e))
			task.returncode = 127
		except Exception as e:
			import traceback
			print(f'Error running command: {task.command}',file=sys.stderr,flush=True)
			print(str(e).split('\n'))
			task.stderr.extend(str(e).split('\n'))
			task.stderr.extend(traceback.format_exc().split('\n'))
			task.returncode = -1
		if not quiet:
			print(pre+'\n'+ '-'*100+post)
			print(pre+f'Process exited with return code {task.returncode}'+post)
		if with_stdErr:
			return task.stdout + task.stderr
		else:
			return task.stdout

def __format_command(command,expand = False):
	'''
	Format a command

	@params:
		command: The command to format
		expand: Whether to expand ranges

	@returns:
		list[list[str]]: The formatted commands sequence
	'''
	if isinstance(command,str):
		if expand:
			commands = _expand_ranges_fast(command)
		else:
			commands = [command]
		return [command.split() for command in commands]
	# elif it is a iterable
	elif hasattr(command,'__iter__'):
		sanitized_command = []
		for field in command:
			if isinstance(field,str):
				sanitized_command.append(field)
			else:
				sanitized_command.append(repr(field))
		if not expand:
			return [sanitized_command]
		sanitized_expanded_command = [_expand_ranges_fast(field) for field in sanitized_command]
		# now the command had been expanded to list of list of fields with each field expanded to all possible options
		# we need to generate all possible combinations of the fields
		# we will use the cartesian product to do this
		commands = list(itertools.product(*sanitized_expanded_command))
		return [list(command) for command in commands]
	else:
		return __format_command(str(command),expand=expand)

#%% core funcitons
def ping(hosts,timeout=1,max_threads=0,quiet=True,dry_run=False,with_stdErr=False,
				return_code_only=False,return_object=False,wait_for_return=True,return_true_false=True):
	'''
	Ping multiple hosts

	@params:
		hosts: The hosts to ping
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
	single_host = False
	if isinstance(hosts,str):
		commands = [f'ping -c 1 {hosts}']
		single_host = True
	else:
		commands = [f'ping -c 1 {host}' for host in hosts]
	if return_true_false:
		return_code_only = True
	results = run_commands(commands, timeout=timeout, max_threads=max_threads, quiet=quiet,
						dry_run=dry_run, with_stdErr=with_stdErr, return_code_only=return_code_only, 
						return_object=return_object,wait_for_return=wait_for_return)
	if return_true_false:
		if single_host:
			return not results[0]
		else:
			return [not result for result in results]
	else:
		if single_host:
			return results[0]
		else:
			return results

def run_command(command, timeout=0,max_threads=1,quiet=False,dry_run=False,with_stdErr=False,
				return_code_only=False,return_object=False,wait_for_return=True, sem = None):
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
		sem: The semaphore to use for threading

	@returns:
		None | int | list[str] | Task: The output of the command
	'''
	return run_commands(commands=[command], timeout=timeout, max_threads=max_threads, quiet=quiet, 
					 dry_run=dry_run, with_stdErr=with_stdErr, return_code_only=return_code_only, 
					 return_object=return_object,parse=False,wait_for_return=wait_for_return,sem=sem)[0]

def run_commands(commands, timeout=0,max_threads=1,quiet=False,dry_run=False,with_stdErr=False,
				 return_code_only=False,return_object=False, parse = False, wait_for_return = True, sem : threading.Semaphore = None):
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
		sem: The semaphore to use for threading

	@returns:
		list: The output of the commands ( list[None] | list[int] | list[list[str]] | list[Task] )
	'''
	# split the commands in commands if it is a string
	formatedCommands = []
	for command in commands:
		formatedCommands.extend(__format_command(command,expand=parse))
	# initialize the tasks
	tasks = [Task(command) for command in formatedCommands]
	# run the tasks with max_threads. if max_threads is 0, use the number of commands
	if max_threads < 1:
		max_threads = len(formatedCommands)
	if max_threads > 1 or not wait_for_return:
		if not sem:
			sem = threading.Semaphore(max_threads)  # Limit concurrent sessions
		threads = [threading.Thread(target=__run_command, args=(task,sem,timeout,quiet,dry_run,...),daemon=True) for task in tasks]
		for thread,task in zip(threads,tasks):
			task.thread = thread
			thread.start()
		if wait_for_return:
			for thread in threads:
				thread.join()
		else:
			__running_threads.update(threads)
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

def join_threads(threads=...,timeout=None):
	'''
	Join threads

	@params:
		threads: The threads to join
		timeout: The timeout

	@returns:
		None
	'''
	global __running_threads
	if threads is ...:
		threads = __running_threads
	for thread in threads:
		thread.join(timeout=timeout)
	if threads is __running_threads:
		__running_threads = {t for t in threads if t.is_alive()}

def main():
	parser = argparse.ArgumentParser(description='Run multiple commands in parallel')
	parser.add_argument('commands', metavar='command', type=str, nargs='+',help='commands to run')
	parser.add_argument('-p','--parse', action='store_true',help='Parse ranged input and expand them into multiple commands')
	parser.add_argument('-t','--timeout', metavar='timeout', type=int, default=60,help='timeout for each command')
	parser.add_argument('-m','--max_threads', metavar='max_threads', type=int, default=1,help='maximum number of threads to use')
	parser.add_argument('-q','--quiet', action='store_true',help='quiet mode')
	parser.add_argument('-V','--version', action='version', version=f'%(prog)s {version} @ {COMMIT_DATE} by pan@zopyr.us')
	args = parser.parse_args()
	run_commands(args.commands, args.timeout, args.max_threads, args.quiet,parse = args.parse, with_stdErr=True)

if __name__ == '__main__':
	main()

#%% misc functions
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

def pretty_format_table(data, delimiter=None, header=None, full=False):
	import re
	version = 1.20
	_ = version
	if not data:
		return ""
	# Normalize input data structure
	if isinstance(data, str):
		data = data.strip("\n").split("\n")
		data = [line.split(delimiter) for line in data]
	elif isinstance(data, dict):
		if isinstance(next(iter(data.values())), dict):
			if not header:
				header = ["key"] + list(next(iter(data.values())).keys())
			data = [[key] + list(value.values()) for key, value in data.items()]
		else:
			data = [[key] + list(value) for key, value in data.items()]
	elif not isinstance(data, list):
		data = list(data)
	if isinstance(data[0], dict):
		if not header:
			header = list(data[0].keys())
		data = [list(item.values()) for item in data]
	elif isinstance(data[0], str):
		data = [row.split(delimiter) for row in data]
	data = [[str(item) for item in row] for row in data]
	if isinstance(header, str):
		header = header.split(delimiter)
	if not header or not any(header):
		header = data[0]
		data = data[1:]
	num_cols = len(header)
	def visible_len(s):
		return len(re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", s))
	# Ensure all rows have the same number of columns
	col_widths = [len(header[i]) for i in range(num_cols)]
	data_invisible_length = []
	for row in data:
		dil = []
		for item, col_width, cwi in zip(row, col_widths, range(num_cols)):
			vl = visible_len(item)
			if vl > col_width:
				col_widths[cwi] = vl
			dil.append(len(item) - vl)
		data_invisible_length.append(dil)
	header_widths = [visible_len(h) for h in header]
	header = [item.ljust(col_width + len(item) - visible_len(item)) for item, col_width in zip(header, col_widths)]
	normalized_padded_data = []
	for row, invisible_lengths in zip(data, data_invisible_length):
		if not any(row):
			row = ['-' * col_width for col_width in col_widths]
		elif len(row) < num_cols:
			row = [row[i].ljust(col_widths[i] + invisible_lengths[i]) if i < len(row) else "".ljust(col_widths[i]) for i in range(num_cols)]
		elif len(row) >= num_cols:
			#row = row[:num_cols]
			row = [item.ljust(col_width + invisible_length) for item, col_width, invisible_length in zip(row, col_widths, invisible_lengths)]
		normalized_padded_data.append(row)
	data = normalized_padded_data
	column_separator = " | "
	horizontal_separator = "-+-"
	terminal_width = get_terminal_size()[0]
	def table_width(col_widths, sep_len):
		return sum(col_widths) + sep_len * (len(col_widths) - 1)
	def render(header, rows, column_widths, column_separator, horizontal_separator):
		return "\n".join(
		 [column_separator.join(header),
		 horizontal_separator.join("-" * width for width in column_widths),
		 *(column_separator.join(row) for row in rows)
		 ]) + "\n"
	if full or table_width(col_widths, len(column_separator)) <= terminal_width:
		return render(header, data, col_widths, column_separator, horizontal_separator)
	# Use compressed separators (no spaces)
	column_separator = "|"
	horizontal_separator = "+"
	if table_width(col_widths, len(column_separator)) <= terminal_width:
		return render(header, data, col_widths, column_separator, horizontal_separator)
	# Begin column compression
	# Track which columns have been compressed already to header width
	width_diff = [max(col_width - header_width, 0) for col_width, header_width in zip(col_widths, header_widths)]
	total_overflow_width = table_width(col_widths, len(column_separator)) - terminal_width
	for i, diff in sorted(enumerate(width_diff), key=lambda x: -x[1]):
		if total_overflow_width <= 0:
			break
		if diff <= 0:
			continue
		reduce_by = min(diff, total_overflow_width)
		col_widths[i] -= reduce_by
		total_overflow_width -= reduce_by
	def truncate_to_width(string, width,invisible_length):
		s_len = len(string) - invisible_length
		if s_len <= width:
			return string
		rstripedSize = len(string.rstrip()) - invisible_length
		if rstripedSize <= width:
			return string[:width + invisible_length]
		if width < 2:
			if width < 1:
				return ""
			else:
				return '.'
		return string[: width + invisible_length - 2] + ".."
	data = [
		[truncate_to_width(item, col_width, invisible_length) for item, col_width, invisible_length in zip(row, col_widths, dil)]
		for row, dil in zip(data, data_invisible_length)
	]
	header = [item[:col_width] for item, col_width in zip(header, col_widths)]
	return render(header, data, col_widths, column_separator, horizontal_separator)

def parseTable(data,sort=False,min_space=2):
	if isinstance(data, str):
		data = data.strip('\n').split('\n')
	header_line = data[0]
	# Use regex to find column names and their positions
	pattern = r'(\S(?:.*?\S)?)(?=\s{'+ str(min_space) + r',}|\s*$)'
	matches = list(re.finditer(pattern, header_line))
	data_list = [[]]
	columns = []
	for i, match in enumerate(matches):
		col_name = match.group(1)
		data_list[0].append(col_name)
		start = match.start()
		if i + 1 < len(matches):
			end = matches[i+1].start()
		else:
			end = None  # Last column goes till the end
		columns.append((col_name, start, end))	
	for line in data[1:]:
		if not line.strip():
			continue  # Skip empty lines
		row = []
		for col_name, start, end in columns:
			if end is not None:
				value = line[start:end].strip()
			else:
				value = line[start:].strip()
			row.append(value)
		data_list.append(row)
	# sort data_list[1:] by the first column
	if sort:
		data_list[1:] = sorted(data_list[1:], key=lambda x: x[0])
	return data_list

def slugify(value, allow_unicode=False):
	import unicodedata
	"""
	Taken from https://github.com/django/django/blob/master/django/utils/text.py
	Convert to ASCII if 'allow_unicode' is False. Convert spaces or repeated
	dashes to single dashes. Remove characters that aren't alphanumerics,
	underscores, or hyphens. Convert to lowercase. Also strip leading and
	trailing whitespace, dashes, and underscores.
	"""
	value = str(value)
	if allow_unicode:
		value = unicodedata.normalize('NFKC', value)
	else:
		value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode('ascii')
	value = re.sub(r'[^\w\s-]', '', value.lower())
	return re.sub(r'[-\s]+', '-', value).strip('-_')

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
	except Exception:
		try:
			import fcntl
			import struct
			import termios
			packed = fcntl.ioctl(0, termios.TIOCGWINSZ, struct.pack('HHHH', 0, 0, 0, 0))
			_tsize = struct.unpack('HHHH', packed)[:2]
		except Exception:
			import shutil
			_tsize = shutil.get_terminal_size(fallback=(240, 50))
	return _tsize

def _genrate_progress_bar(iteration, total, prefix='', suffix='',columns=240):
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
			lineOut += ' Calculating... '
	if not noSuffix:
		lineOut += suffix
	return lineOut

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
	except Exception:
		if iteration % 5 == 0:
			print(_genrate_progress_bar(iteration, total, prefix, suffix))

def format_bytes(size, use_1024_bytes=None, to_int=False, to_str=False, str_format=".2f"):
	if to_int or isinstance(size, str):
		if isinstance(size, int):
			return size
		elif isinstance(size, str):
			match = re.match(r"(\d+(\.\d+)?)\s*([a-zA-Z]*)", size)
			if not match:
				if to_str:
					return size
				print(
					"Invalid size format. Expected format: 'number [unit]', "
					"e.g., '1.5 GiB' or '1.5GiB'"
				)
				print(f"Got: {size}")
				return 0
			number, _, unit = match.groups()
			number = float(number)
			unit = unit.strip().lower().rstrip("b")
			if unit.endswith("i"):
				use_1024_bytes = True
			elif use_1024_bytes is None:
				use_1024_bytes = False
			unit = unit.rstrip("i")
			if use_1024_bytes:
				power = 2**10
			else:
				power = 10**3
			unit_labels = {
				"": 0,
				"k": 1,
				"m": 2,
				"g": 3,
				"t": 4,
				"p": 5,
				"e": 6,
				"z": 7,
				"y": 8,
			}
			if unit not in unit_labels:
				if to_str:
					return size
			else:
				if to_str:
					return format_bytes(
						size=int(number * (power**unit_labels[unit])),
						use_1024_bytes=use_1024_bytes,
						to_str=True,
						str_format=str_format,
					)
				return int(number * (power**unit_labels[unit]))
		else:
			try:
				return int(size)
			except Exception:
				return 0
	elif to_str or isinstance(size, int) or isinstance(size, float):
		if isinstance(size, str):
			try:
				size = size.rstrip("B").rstrip("b")
				size = float(size.lower().strip())
			except Exception:
				return size
		if use_1024_bytes or use_1024_bytes is None:
			power = 2**10
			n = 0
			power_labels = {
				0: "",
				1: "Ki",
				2: "Mi",
				3: "Gi",
				4: "Ti",
				5: "Pi",
				6: "Ei",
				7: "Zi",
				8: "Yi",
			}
			while size > power:
				size /= power
				n += 1
			return f"{size:{str_format}} {' '}{power_labels[n]}".replace("  ", " ")
		else:
			power = 10**3
			n = 0
			power_labels = {
				0: "",
				1: "K",
				2: "M",
				3: "G",
				4: "T",
				5: "P",
				6: "E",
				7: "Z",
				8: "Y",
			}
			while size > power:
				size /= power
				n += 1
			return f"{size:{str_format}} {' '}{power_labels[n]}".replace("  ", " ")
	else:
		try:
			return format_bytes(float(size), use_1024_bytes)
		except Exception:
			pass
		return 0

