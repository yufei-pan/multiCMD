#!/usr/bin/env python3
# /// script
# requires-python = ">=3.6"
# dependencies = [
#     "argparse",
# ]
# ///
import time
import threading
import io
import argparse
import sys
import subprocess
import select
import os
import string
import re
import itertools
import signal

version = '1.32'
__version__ = version

__running_threads = []
__variables = {}
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

# immutable helpers compiled once at import time
_BRACKET_RX   = re.compile(r'\[([^\]]+)\]')
_ALPHANUM     = string.digits + string.ascii_letters
_ALPHA_IDX    = {c: i for i, c in enumerate(_ALPHANUM)}

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

def _expand_ranges(inStr):
	'''
	Expand ranges in a string

	@params:
		inStr: The string to expand

	@returns:
		list[str]: The expanded string
	'''
	global __variables
	expandingStr = [inStr]
	expandedList = []
	# all valid alphanumeric characters
	alphanumeric = string.digits + string.ascii_letters
	while len(expandingStr) > 0:
		currentStr = expandingStr.pop()
		match = re.search(r'\[(.*?)]', currentStr)
		if not match:
			expandedList.append(currentStr)
			continue
		group = match.group(1)
		parts = group.split(',')
		for part in parts:
			part = part.strip()
			if ':' in part:
				variableName, _, part = part.partition(':')
				__variables[variableName] = part
				expandingStr.append(currentStr.replace(match.group(0), '', 1))
			elif '-' in part:
				try:
					range_start,_, range_end = part.partition('-')
				except ValueError:
					expandedList.append(currentStr)
					continue
				range_start = range_start.strip()
				if range_start in __variables:
					range_start = __variables[range_start]
				range_end = range_end.strip()
				if range_end in __variables:
					range_end = __variables[range_end]
				if range_start.isdigit() and range_end.isdigit():
					padding_length = min(len(range_start), len(range_end))
					format_str = "{:0" + str(padding_length) + "d}"
					for i in range(int(range_start), int(range_end) + 1):
						formatted_i = format_str.format(i)
						expandingStr.append(currentStr.replace(match.group(0), formatted_i, 1))
				elif all(c in string.hexdigits for c in range_start + range_end):
					for i in range(int(range_start, 16), int(range_end, 16) + 1):
						expandingStr.append(currentStr.replace(match.group(0), format(i, 'x'), 1))
				else:
					try:
						start_index = alphanumeric.index(range_start)
						end_index = alphanumeric.index(range_end)
						for i in range(start_index, end_index + 1):
							expandingStr.append(currentStr.replace(match.group(0), alphanumeric[i], 1))
					except ValueError:
						expandedList.append(currentStr)
			else:
				expandingStr.append(currentStr.replace(match.group(0), part, 1))
	expandedList.reverse()
	return expandedList

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
			print(f'Command / path not found: {task.command[0]}',file=sys.stderr,flush=True)
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
			__running_threads.extend(threads)
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

def join_threads(threads=__running_threads,timeout=None):
	'''
	Join threads

	@params:
		threads: The threads to join
		timeout: The timeout

	@returns:
		None
	'''
	for thread in threads:
		thread.join(timeout=timeout)

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
	parser.add_argument('-p','--parse', action='store_true',help='Parse ranged input and expand them into multiple commands')
	parser.add_argument('-t','--timeout', metavar='timeout', type=int, default=60,help='timeout for each command')
	parser.add_argument('-m','--max_threads', metavar='max_threads', type=int, default=1,help='maximum number of threads to use')
	parser.add_argument('-q','--quiet', action='store_true',help='quiet mode')
	parser.add_argument('-V','--version', action='version', version=f'%(prog)s {version} by pan@zopyr.us')
	args = parser.parse_args()
	run_commands(args.commands, args.timeout, args.max_threads, args.quiet,parse = args.parse, with_stdErr=True)

if __name__ == '__main__':
	main()