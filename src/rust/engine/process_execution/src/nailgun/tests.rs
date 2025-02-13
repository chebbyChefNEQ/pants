use std::path::PathBuf;

use store::Store;
use task_executor::Executor;
use tempfile::TempDir;
use testutil::owned_string_vec;

use crate::nailgun::NailgunPool;
use crate::Process;

fn pool(size: usize) -> NailgunPool {
  let store_dir = TempDir::new().unwrap();
  let executor = Executor::new();
  let store = Store::local_only(executor.clone(), store_dir.path()).unwrap();
  NailgunPool::new(std::env::temp_dir(), size, store, executor)
}

async fn run(pool: &NailgunPool, port: u16) -> PathBuf {
  let mut p = pool
    .acquire(Process::new(owned_string_vec(&[
      "/bin/bash",
      "-c",
      &format!("echo Mock port {}.; sleep 10", port),
    ])))
    .await
    .unwrap();
  assert_eq!(port, p.port());
  let workdir = p.workdir_path().to_owned();
  p.release().await.unwrap();
  workdir
}

#[tokio::test]
async fn acquire() {
  let pool = pool(1);

  // Sequential calls with the same fingerprint reuse the entry.
  let workdir_one = run(&pool, 100).await;
  let workdir_two = run(&pool, 100).await;
  assert_eq!(workdir_one, workdir_two);

  // A call with a different fingerprint launches in a new workdir and succeeds.
  let workdir_three = run(&pool, 200).await;
  assert_ne!(workdir_two, workdir_three);
}
