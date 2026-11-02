use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Arc, Condvar, Mutex, Weak};

#[derive(Default)]
pub(crate) struct CallbackRegistry {
    closing_pid: AtomicU32,
    callbacks: Mutex<Vec<Weak<Callbacks>>>,
}

impl CallbackRegistry {
    pub(crate) fn is_closing(&self) -> bool {
        self.closing_pid.load(Ordering::Acquire) == std::process::id()
    }

    // Registration and closing are called with the GIL held. Callback threads
    // never take this lock, so a fork cannot inherit it from one of those threads.
    pub(crate) fn register(&self) -> Option<Arc<Callbacks>> {
        let mut callbacks = self.callbacks.lock().unwrap();
        if self.is_closing() {
            return None;
        }
        callbacks.retain(|entry| entry.strong_count() != 0);
        // Interpreter shutdown is shared: a callback on one runtime can be
        // waiting for an operation already issued on another runtime.
        if let Some(entry) = callbacks
            .iter()
            .filter_map(Weak::upgrade)
            .find(|entry| entry.pid == std::process::id())
        {
            return Some(entry);
        }
        let entry = Arc::new(Callbacks {
            pid: std::process::id(),
            state: Mutex::new(State::default()),
            changed: Condvar::new(),
        });
        callbacks.push(Arc::downgrade(&entry));
        Some(entry)
    }

    pub(crate) fn begin_shutdown(&self) -> Vec<Arc<Callbacks>> {
        self.closing_pid
            .store(std::process::id(), Ordering::Release);
        let callbacks: Vec<_> = self
            .callbacks
            .lock()
            .unwrap()
            .iter()
            .filter_map(Weak::upgrade)
            // Inherited callback counts/locks belong to threads in the parent.
            .filter(|entry| entry.pid == std::process::id())
            .collect();
        for entry in &callbacks {
            entry.close();
        }
        callbacks
    }
}

pub(crate) struct Callbacks {
    pid: u32,
    state: Mutex<State>,
    changed: Condvar,
}

#[derive(Default)]
struct State {
    closed: bool,
    active: usize,
}

impl Callbacks {
    pub(crate) fn enter(self: &Arc<Self>) -> Option<CallbackGuard> {
        self.enter_inner(false)
    }

    pub(crate) fn enter_completion(self: &Arc<Self>) -> Option<CallbackGuard> {
        self.enter_inner(true)
    }

    fn enter_inner(self: &Arc<Self>, completion: bool) -> Option<CallbackGuard> {
        if self.pid != std::process::id() {
            return None;
        }
        let mut state = self.state.lock().unwrap();
        // An active callback may be awaiting an already-issued RPC on another
        // runtime. Drain its completions, but do not admit work after quiescence.
        if state.closed && (!completion || state.active == 0) {
            return None;
        }
        state.active += 1;
        Some(CallbackGuard(self.clone()))
    }

    fn close(&self) {
        let mut state = self.state.lock().unwrap();
        state.closed = true;
        self.changed.notify_all();
    }

    pub(crate) fn wait(&self) {
        let mut state = self.state.lock().unwrap();
        while state.active != 0 {
            state = self.changed.wait(state).unwrap();
        }
    }
}

pub(crate) struct CallbackGuard(Arc<Callbacks>);

impl Drop for CallbackGuard {
    fn drop(&mut self) {
        let mut state = self.0.state.lock().unwrap();
        state.active -= 1;
        if state.closed && state.active == 0 {
            self.0.changed.notify_all();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc::{channel, RecvTimeoutError};
    use std::thread;
    use std::time::Duration;

    #[test]
    fn shutdown_waits_for_callbacks_and_rejects_new_work() {
        let registry = Arc::new(CallbackRegistry::default());
        let callbacks = registry.register().unwrap();
        let guard = callbacks.enter().unwrap();
        let (closed_tx, closed_rx) = channel();
        let (done_tx, done_rx) = channel();
        let closing_registry = registry.clone();
        let closer = thread::spawn(move || {
            let callbacks = closing_registry.begin_shutdown();
            closed_tx.send(()).unwrap();
            for entry in callbacks {
                entry.wait();
            }
            done_tx.send(()).unwrap();
        });

        closed_rx.recv_timeout(Duration::from_secs(5)).unwrap();
        assert!(registry.register().is_none());
        assert!(callbacks.enter().is_none());
        assert_eq!(
            done_rx.recv_timeout(Duration::from_millis(50)),
            Err(RecvTimeoutError::Timeout)
        );
        drop(guard);
        done_rx.recv_timeout(Duration::from_secs(5)).unwrap();
        closer.join().unwrap();
        for entry in registry.begin_shutdown() {
            entry.wait();
        }
    }

    #[test]
    fn shutdown_ignores_inherited_callback_locks_and_counts() {
        let registry = CallbackRegistry::default();
        let inherited = Arc::new(Callbacks {
            pid: std::process::id().wrapping_add(1),
            state: Mutex::new(State {
                closed: false,
                active: 1,
            }),
            changed: Condvar::new(),
        });
        registry
            .callbacks
            .lock()
            .unwrap()
            .push(Arc::downgrade(&inherited));
        let _inherited_lock = inherited.state.lock().unwrap();
        assert!(inherited.enter().is_none());
        assert!(registry.begin_shutdown().is_empty());
    }

    #[test]
    fn active_callbacks_can_drain_completions_from_other_runtimes() {
        let registry = CallbackRegistry::default();
        let first_runtime = registry.register().unwrap();
        let second_runtime = registry.register().unwrap();
        let active = first_runtime.enter().unwrap();
        let closing = registry.begin_shutdown();
        assert!(second_runtime.enter().is_none());
        let completion = second_runtime.enter_completion().unwrap();
        drop(active);
        assert!(first_runtime.enter_completion().is_some());
        drop(completion);
        for entry in closing {
            entry.wait();
        }
        assert!(second_runtime.enter_completion().is_none());
    }

    #[test]
    fn shutdown_races_safely_with_callback_entry() {
        let registry = CallbackRegistry::default();
        let callbacks = registry.register().unwrap();
        let ready = Arc::new(std::sync::Barrier::new(9));
        let threads: Vec<_> = (0..8)
            .map(|_| {
                let callbacks = callbacks.clone();
                let ready = ready.clone();
                thread::spawn(move || {
                    ready.wait();
                    drop(callbacks.enter());
                })
            })
            .collect();
        ready.wait();
        let closing = registry.begin_shutdown();
        for entry in closing {
            entry.wait();
        }
        for thread in threads {
            thread.join().unwrap();
        }
        assert!(callbacks.enter().is_none());
    }
}
