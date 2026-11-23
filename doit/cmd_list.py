from .cmd_base import DoitCmdBase, check_tasks_exist, subtasks_iter

opt_listall = {
    'name': 'subtasks',
    'short': '',
    'long': 'all',
    'type': bool,
    'default': False,
    'help': "list include all sub-tasks from dodo file"
}

opt_list_quiet = {
    'name': 'quiet',
    'short': 'q',
    'long': 'quiet',
    'type': bool,
    'default': False,
    'help': 'print just task name (less verbose than default)'
}

opt_list_status = {
    'name': 'status',
    'short': 's',
    'long': 'status',
    'type': bool,
    'default': False,
    'help': 'print task status (R)un, (U)p-to-date, (I)gnored'
}

opt_list_private = {
    'name': 'private',
    'short': 'p',
    'long': 'private',
    'type': bool,
    'default': False,
    'help': "print private tasks (start with '_')"
}

opt_list_dependencies = {
    'name': 'list_deps',
    'short': '',
    'long': 'deps',
    'type': bool,
    'default': False,
    'help': ("print list of dependencies "
             "(file dependencies only)")
}

opt_template = {
    'name': 'template',
    'short': '',
    'long': 'template',
    'type': str,
    'default': None,
    'help': "display entries with template"
}

opt_sort = {
    'name': 'sort',
    'short': '',
    'long': 'sort',
    'type': str,
    'choices': [('name', 'sort by task name'),
                ('definition', 'list tasks in the order they were defined')],
    'default': 'name',
    'help': ("choose the manner in which the task list is sorted. "
             "[default: %(default)s]")
}

opt_list_manifest = {
    'name': 'manifest',
    'short': '',
    'long': 'manifest',
    'type': str,
    'default': None,
    'help': ("show tasks/results for a manifest version. Tasks whose saved "
             "result can not be proven to belong to the current graph are "
             "listed separately. [default: none]")
}


class List(DoitCmdBase):
    doc_purpose = "list tasks from dodo file"
    doc_usage = "[TASK ...]"
    doc_description = None

    cmd_options = (opt_listall, opt_list_quiet, opt_list_status,
                   opt_list_private, opt_list_dependencies, opt_template,
                   opt_sort, opt_list_manifest)


    STATUS_MAP = {'ignore': 'I', 'up-to-date': 'U', 'run': 'R', 'error': 'E'}


    def _print_task(self, template, task, status, list_deps, tasks):
        """print a single task"""
        line_data = {'name': task.name, 'doc': task.doc}
        # FIXME group task status is never up-to-date
        if status:
            # FIXME: 'ignore' handling is ugly
            if self.dep_manager.status_is_ignore(task):
                task_status = 'ignore'
            else:
                task_status = self.dep_manager.get_status(task, tasks).status
            line_data['status'] = self.STATUS_MAP[task_status]

        self.outstream.write(template.format(**line_data))

        # print dependencies
        if list_deps:
            for dep in task.file_dep:
                self.outstream.write(" -  %s\n" % dep)
            self.outstream.write("\n")

    @staticmethod
    def _list_filtered(tasks, filter_tasks, include_subtasks):
        """return list of task based on selected 'filter_tasks' """
        check_tasks_exist(tasks, filter_tasks)

        # get task by name
        print_list = []
        for name in filter_tasks:
            task = tasks[name]
            print_list.append(task)
            if include_subtasks:
                print_list.extend(subtasks_iter(tasks, task))
        return print_list


    def _list_all(self, include_subtasks):
        """list of tasks"""
        print_list = []
        for task in self.task_list:
            if (not include_subtasks) and task.subtask_of:
                continue
            print_list.append(task)
        return print_list


    def _execute(self, subtasks=False, quiet=True, status=False, private=False,
                 list_deps=False, template=None, sort='name', pos_args=None,
                 manifest=None):
        """List task generators"""
        filter_tasks = pos_args
        # dict of all tasks
        tasks = dict([(t.name, t) for t in self.task_list])

        if manifest:
            from . import manifest as manifest_lib
            selected, _reserved = manifest_lib.select_tasks(
                self.task_list, manifest)
            tasks = dict([(t.name, t) for t in selected])
            self.dep_manager.manifest_mode = True
            self.dep_manager.manifest_id = manifest
            for task in tasks.values():
                self.dep_manager.register_task(task)

        if filter_tasks:
            # list only tasks passed on command line
            print_list = self._list_filtered(tasks, filter_tasks, subtasks)
        else:
            print_list = self._list_all(subtasks)

        # exclude private tasks
        if not private:
            print_list = [t for t in print_list if not t.name.startswith('_')]

        # set template
        if template is None:
            max_name_len = 0
            if print_list:
                max_name_len = max(len(t.name) for t in print_list)

            template = '{name:<' + str(max_name_len + 3) + '}'
            if not quiet:
                template += '{doc}'
            if status:
                template = '{status} ' + template
        template += '\n'

        # sort list of tasks
        if sort == 'name':
            print_list = sorted(print_list)
        elif sort == 'definition':
            pass  # task list is already sorted in order of definition

        # print list of tasks
        for task in print_list:
            self._print_task(template, task, status, list_deps, tasks)

        if manifest:
            self._print_unconfirmed(print_list)
        return 0

    def _print_unconfirmed(self, print_list):
        """list tasks whose saved result can not be proven for this graph

        Legacy data only carries a name and value, and a changed graph makes
        the old receipt unusable.  They are listed apart so they are never
        mistaken for a successful update.
        """
        pending = []
        for task in print_list:
            try:
                state, reason = self.dep_manager.manifest_check(task)
            except Exception:  # pragma: no cover - defensive
                continue
            if state == 'unconfirmed':
                pending.append((task, reason))
        if not pending:
            return
        self.outstream.write('\nneeds confirmation:\n')
        for task, reason in sorted(pending, key=lambda item: item[0].name):
            self.outstream.write("  {} ({})\n".format(task.name, reason))
